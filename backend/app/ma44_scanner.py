from __future__ import annotations

import json
import threading
import time
from datetime import date, datetime, time as dt_time, timedelta, timezone
from pathlib import Path

from app.market_data_master import configured_source, market_data_master

# Termux installations may not include the system IANA tzdata package.
# India uses a fixed UTC+05:30 offset year-round, so avoid a ZoneInfo
# dependency for the scanner's market-session clock.
IST = timezone(timedelta(hours=5, minutes=30))
MARKET_OPEN = dt_time(9, 15)
MARKET_CLOSE = dt_time(15, 30)
EOD_START = dt_time(15, 35)
REFRESH_SECONDS = 60
HISTORY_LIMIT = 260
TREND_POINTS = 21


def _now() -> datetime:
    return datetime.now(IST)


def _is_weekday(day: date) -> bool:
    return day.weekday() < 5


def _market_open(now: datetime) -> bool:
    return _is_weekday(now.date()) and MARKET_OPEN <= now.time() <= MARKET_CLOSE


def _after_market(now: datetime) -> bool:
    return _is_weekday(now.date()) and now.time() >= EOD_START


def _distance(price: float, ma: float) -> float:
    return ((price - ma) / ma) * 100.0 if ma else 0.0


class MA44Scanner:
    """Background 44 SMA scanner backed by the primary market-data master.

    Broker/FYERS connections are never required. Live mode refreshes primary
    quotes; EOD mode evaluates the completed daily candle from the same source.
    Historical calculations are cached in memory.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._running_key: str | None = None
        self._results: dict[str, list[dict]] = {"live": [], "eod": []}
        self._metrics: dict[str, dict] = {}
        self._universe: list[tuple[str, str]] = []
        self._last_date: str | None = None
        self._eod_done_date: str | None = None
        self._last_live_scan: float | None = None
        self._last_eod_scan: float | None = None
        self._status = "IDLE"
        self._stage = "WAITING"
        self._message = "44 MA scanner waiting for primary market data."
        self._error = ""
        self._processed = 0
        self._total = 0
        self._account_id: int | None = None


    @staticmethod
    def _metrics_cache_path() -> Path:
        return Path(__file__).resolve().parent / "ma44_metrics_cache.json"

    def _load_metrics_cache(self, current_date: date) -> dict[str, dict]:
        path = self._metrics_cache_path()
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            cached_date = date.fromisoformat(str(payload.get("as_of", "")))
            metrics = payload.get("metrics")
            if cached_date >= current_date or not isinstance(metrics, dict):
                return {}
            clean: dict[str, dict] = {}
            for ticker, metric in metrics.items():
                if not isinstance(metric, dict):
                    continue
                if (
                    len(metric.get("tail_closes") or []) >= 199
                    and len(metric.get("trend_ma44") or []) == TREND_POINTS
                    and len(metric.get("trend_ma150") or []) == TREND_POINTS
                    and len(metric.get("trend_ma200") or []) == TREND_POINTS
                ):
                    clean[str(ticker).upper()] = metric
            return clean
        except Exception:
            return {}

    def _save_metrics_cache(self, metrics: dict[str, dict]) -> None:
        path = self._metrics_cache_path()
        tmp = path.with_suffix(".tmp")
        payload = {
            "as_of": (_now().date() - timedelta(days=1)).isoformat(),
            "metrics": metrics,
        }
        try:
            tmp.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
            tmp.replace(path)
        except Exception:
            pass

    def start(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run,
                name="pipsgox-ma44-scanner",
                daemon=True,
            )
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def set_preferred_account(self, account_id: int | None) -> None:
        # Compatibility no-op. Market data is not account-bound.
        with self._lock:
            self._account_id = None

    def _load_universe(self) -> list[tuple[str, str]]:
        # Use the latest NSE equity bhavcopy only as a lightweight stock-universe
        # catalogue. Price/history calculations remain on the primary source.
        from app.nse_bhavcopy import fetch_latest

        _, rows = fetch_latest()
        return [(ticker, ticker) for ticker in sorted(rows)]

    @staticmethod
    def _metrics_from_candles(candles, *, include_last_day: bool) -> dict | None:
        if len(candles) < 221:
            return None

        candles = sorted(candles, key=lambda item: item.time)
        closes = [float(c.close) for c in candles if float(c.close) > 0]
        if len(closes) < 221:
            return None

        ma44 = []
        ma150 = []
        ma200 = []
        for i in range(200, len(closes)):
            ma44.append(sum(closes[i - 43:i + 1]) / 44.0)
            ma150.append(sum(closes[i - 149:i + 1]) / 150.0)
            ma200.append(sum(closes[i - 199:i + 1]) / 200.0)

        if len(ma44) < TREND_POINTS:
            return None

        last44 = ma44[-TREND_POINTS:]
        last150 = ma150[-TREND_POINTS:]
        last200 = ma200[-TREND_POINTS:]

        rising = (
            all(last44[i] > last44[i - 1] for i in range(1, TREND_POINTS))
            and all(last150[i] > last150[i - 1] for i in range(1, TREND_POINTS))
            and all(last200[i] > last200[i - 1] for i in range(1, TREND_POINTS))
        )
        ordered = all(
            last44[i] > last150[i] > last200[i]
            for i in range(TREND_POINTS)
        )
        if not (rising and ordered):
            return None

        return {
            "sma44": last44[-1],
            "sma150": last150[-1],
            "sma200": last200[-1],
            "trend_ma44": last44,
            "trend_ma150": last150,
            "trend_ma200": last200,
            "tail_closes": closes[-199:],
            "trend_valid": True,
            "last_close": closes[-1],
        }

    def _history_metrics(self, provider, api_symbol: str, include_last_day: bool) -> dict | None:
        candles = provider.get_history(
            api_symbol,
            "D",
            HISTORY_LIMIT,
            start=None,
            end=date.today(),
        )
        if not include_last_day:
            today = _now().date()
            candles = [candle for candle in candles if datetime.fromtimestamp(candle.time, IST).date() < today]
        return self._metrics_from_candles(candles, include_last_day=include_last_day)

    def _load_metrics(self, provider, universe: list[tuple[str, str]], include_last_day: bool) -> None:
        with self._lock:
            self._processed = 0
            self._total = len(universe)
            self._status = "PREPARING"
            self._stage = "BUILDING_TREND"
            self._message = f"Building 44/150/200 SMA trend data · 0/{len(universe):,}"
            self._error = ""

        fresh: dict[str, dict] = {}
        chunk_size = 40

        for offset in range(0, len(universe), chunk_size):
            if self._stop.is_set():
                return

            chunk = universe[offset:offset + chunk_size]
            try:
                history_map = provider.get_history_batch(
                    [api for _, api in chunk],
                    "D",
                    HISTORY_LIMIT,
                    start=None,
                    end=date.today(),
                )
            except Exception as exc:
                history_map = {}
                batch_error = str(exc)
            else:
                batch_error = ""

            for ticker, api in chunk:
                try:
                    candles = history_map.get(api.upper())
                    if candles and not include_last_day:
                        today = _now().date()
                        candles = [
                            candle for candle in candles
                            if datetime.fromtimestamp(candle.time, IST).date() < today
                        ]
                    metric = self._metrics_from_candles(candles or [], include_last_day=include_last_day)
                    if metric:
                        fresh[ticker] = metric
                except Exception:
                    # One malformed/delisted security must not abort the entire batch.
                    pass

            with self._lock:
                self._processed = min(offset + len(chunk), self._total)
                self._message = (
                    f"Building 44/150/200 SMA trend data · "
                    f"{self._processed:,}/{self._total:,}"
                )
                if batch_error:
                    self._error = f"Some market-data batches failed: {batch_error}"

        self._save_metrics_cache(fresh)
        with self._lock:
            self._metrics = fresh
            self._stage = "READY"
            self._message = f"Trend data ready · {len(fresh):,} stocks passed the 20-day trend filter."
            self._status = "READY"


    @staticmethod
    def _provider():
        return market_data_master

    def _scan_live(self, provider) -> None:
        with self._lock:
            universe = list(self._universe)
            metrics = dict(self._metrics)
        if not universe or not metrics:
            return

        quotes = provider.get_quotes([api for _, api in universe])
        api_to_ticker = {api.upper(): ticker for ticker, api in universe}
        results: list[dict] = []
        for quote in quotes:
            ticker = api_to_ticker.get(quote.symbol.upper())
            metric = metrics.get(ticker or "")
            if not metric or quote.last is None or quote.open is None or quote.low is None:
                continue
            tail = metric.get("tail_closes") or []
            if len(tail) < 43:
                continue

            # LIVE treats the current price as today's running close and
            # calculates today's 44 SMA from the previous 43 completed closes
            # plus the current price.
            current_close = float(quote.last)
            sma44 = (sum(tail[-43:]) + current_close) / 44.0
            low_distance = _distance(float(quote.low), sma44)

            # Loosened filters:
            # 1) current price > current-day 44 SMA
            # 2) today's low is -0.25% to +2.00% from current-day 44 SMA
            # 3) today's candle is bullish: current price > today's open
            if current_close <= sma44:
                continue
            if not (-0.25 <= low_distance <= 2.0):
                continue
            if current_close <= float(quote.open):
                continue

            results.append({
                "symbol": ticker,
                "closed": current_close,
                "change_percent": quote.change_percent,
                "ma_distance": _distance(current_close, sma44),
                "low_distance": low_distance,
                "sma44": sma44,
                "high": quote.high,
                "low": quote.low,
                "mode": "live",
            })

        results.sort(key=lambda row: row["symbol"])
        with self._lock:
            self._results["live"] = results
            self._last_live_scan = time.time()
            self._status = "LIVE"
            self._message = f"Live scan active · {len(results)} matches"

    def _scan_eod(self, provider) -> None:
        with self._lock:
            universe = list(self._universe)
            metrics = dict(self._metrics)
            self._processed = 0
            self._total = len(universe)
            self._status = "SCANNING"
            self._stage = "SCANNING_EOD"
            self._message = f"EOD scan in progress · 0/{len(universe):,}"
            self._error = ""

        if not metrics:
            with self._lock:
                self._stage = "BUILDING_TREND"
                self._message = "Preparing EOD trend data from completed daily candles..."
            self._load_metrics(provider, universe, include_last_day=False)
            with self._lock:
                metrics = dict(self._metrics)
                self._stage = "SCANNING_EOD"
                self._status = "SCANNING"
                self._processed = 0
                self._total = len(universe)
                self._message = f"EOD scan in progress · 0/{len(universe):,}"

        # EOD uses the same primary quote source as charts/watchlists.
        quotes = provider.get_quotes([api for _, api in universe])
        api_to_quote = {quote.symbol.upper(): quote for quote in quotes}
        results: list[dict] = []

        for processed, (ticker, api) in enumerate(universe, start=1):
            if self._stop.is_set():
                return
            metric = metrics.get(ticker)
            quote = api_to_quote.get(api.upper())
            if not metric or quote is None:
                continue

            today_open = float(quote.open or 0)
            today_high = float(quote.high or 0)
            today_low = float(quote.low or 0)
            today_close = float(quote.last or 0)
            previous_close = today_close - float(quote.change or 0)

            tail = metric.get("tail_closes") or []
            if len(tail) < 199 or today_close <= 0:
                continue

            sma44 = (sum(tail[-43:]) + today_close) / 44.0
            sma150 = (sum(tail[-149:]) + today_close) / 150.0
            sma200 = (sum(tail[-199:]) + today_close) / 200.0

            trend44 = (list(metric["trend_ma44"]) + [sma44])[-TREND_POINTS:]
            trend150 = (list(metric["trend_ma150"]) + [sma150])[-TREND_POINTS:]
            trend200 = (list(metric["trend_ma200"]) + [sma200])[-TREND_POINTS:]
            rising = (
                all(trend44[i] > trend44[i - 1] for i in range(1, TREND_POINTS))
                and all(trend150[i] > trend150[i - 1] for i in range(1, TREND_POINTS))
                and all(trend200[i] > trend200[i - 1] for i in range(1, TREND_POINTS))
            )
            ordered = all(
                trend44[i] > trend150[i] > trend200[i]
                for i in range(TREND_POINTS)
            )

            low_distance = _distance(today_low, sma44)
            close_distance = _distance(today_close, sma44)

            if (
                rising
                and ordered
                and today_close > today_open
                and today_close > sma44
                and -0.25 <= low_distance <= 2.0
            ):
                results.append({
                    "symbol": ticker,
                    "closed": today_close,
                    "change_percent": quote.change_percent,
                    "ma_distance": close_distance,
                    "sma44": sma44,
                    "high": today_high,
                    "low": today_low,
                    "mode": "eod",
                    "market_data_source": configured_source(),
                })

            with self._lock:
                self._processed = processed
                self._message = (
                    f"EOD scan in progress · {processed:,}/{len(universe):,} "
                    f"· {len(results):,} matches"
                )

        results.sort(key=lambda row: row["symbol"])
        with self._lock:
            self._results["eod"] = results
            self._last_eod_scan = time.time()
            self._eod_done_date = _now().date().isoformat()
            self._stage = "COMPLETE"
            self._status = "EOD_COMPLETE"
            self._message = (
                f"EOD scan complete · {len(results):,} matches · "
                f"primary market data {configured_source()}"
            )

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                now = _now()

                with self._lock:
                    if self._last_date != now.date().isoformat():
                        self._last_date = now.date().isoformat()
                        self._metrics = self._load_metrics_cache(now.date())
                        self._results["live"] = []
                        self._results["eod"] = []
                        self._eod_done_date = None

                provider = self._provider()
                with self._lock:
                    cached_universe = list(self._universe)
                if cached_universe:
                    universe = cached_universe
                else:
                    with self._lock:
                        self._status = "PREPARING"
                        self._stage = "LOADING_UNIVERSE"
                        self._message = "Loading NSE stock universe..."
                    universe = self._load_universe()
                with self._lock:
                    self._universe = universe
                    self._account_id = None
                    if self._stage == "LOADING_UNIVERSE":
                        self._message = f"NSE universe loaded · {len(universe):,} stocks"

                if _market_open(now):
                    if not self._metrics:
                        self._load_metrics(provider, universe, include_last_day=False)
                    if self._stop.is_set():
                        break
                    self._scan_live(provider)
                    self._stop.wait(REFRESH_SECONDS)
                    continue

                if _after_market(now):
                    if self._eod_done_date != now.date().isoformat():
                        self._scan_eod(provider)
                    else:
                        with self._lock:
                            self._stage = "COMPLETE"
                    self._status = "EOD_COMPLETE"
                    self._stop.wait(30)
                    continue

                with self._lock:
                    self._status = "WAITING"
                    self._stage = "WAITING_FOR_MARKET"
                    self._message = f"Waiting for NSE market session · source {configured_source()}"
                self._stop.wait(30)
            except Exception as exc:
                with self._lock:
                    self._status = "ERROR"
                    self._stage = "ERROR"
                    self._error = str(exc)
                    self._message = "44 MA scanner encountered an error; retrying."
                self._stop.wait(30)

    def snapshot(self, mode: str = "live", account_id: int | None = None) -> dict:
        if account_id is not None:
            self.set_preferred_account(account_id)
        mode = "eod" if mode == "eod" else "live"
        with self._lock:
            return {
                "mode": mode,
                "status": self._status,
                "stage": self._stage,
                "message": self._message,
                "progress": (
                    round((self._processed / self._total) * 100, 1)
                    if self._total else 0
                ),
                "error": self._error,
                "account_id": None,
                "universe_count": len(self._universe),
                "eligible_trend_count": len(self._metrics),
                "processed": self._processed,
                "total": self._total,
                "last_scan": self._last_live_scan if mode == "live" else self._last_eod_scan,
                "results": list(self._results[mode]),
            }


scanner = MA44Scanner()
