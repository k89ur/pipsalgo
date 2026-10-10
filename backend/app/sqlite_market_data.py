from __future__ import annotations

"""Local SQLite store for NSE end-of-day OHLCV.

Daily rows are sourced from NSE's official CM bhavcopy archives. No broker or
third-party market-data SDK is used. Intraday bars are intentionally unsupported.
"""

import csv
import io
import sqlite3
import threading
import time
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

import requests

from app.providers.base import Candle, Quote
from app.corporate_actions import (
    adjust_candles, initialize_corporate_actions, action_status,
    refresh_nse_corporate_actions,
)

IST = timezone(timedelta(hours=5, minutes=30))
# Five calendar years of EOD history, including leap days.
HISTORY_DAYS = 1826
BASE_URL = "https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_{date}_F_0000.csv.zip"
LEGACY_BASE_URL = "https://archives.nseindia.com/content/historical/EQUITIES/{year}/{month}/cm{day}{month}{year}bhav.csv.zip"
UDIFF_CUTOVER = date(2024, 7, 8)
INDEX_BASE_URL = "https://archives.nseindia.com/content/indices/ind_close_all_{date}.csv"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/153 Safari/537.36",
    "Accept": "application/zip,text/csv,*/*",
    "Referer": "https://www.nseindia.com/",
}
DB_PATH = Path(__file__).resolve().parents[2] / ".pipsgox" / "market_data.sqlite3"
DB_PATH_OVERRIDE = "PIPSGOX_MARKET_DB"
_sync_lock = threading.RLock()


def _db_path() -> Path:
    configured = __import__("os").getenv(DB_PATH_OVERRIDE, "").strip()
    path = Path(configured).expanduser() if configured else DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _connect() -> sqlite3.Connection:
    connection = sqlite3.connect(str(_db_path()), timeout=30)
    connection.row_factory = sqlite3.Row
    # WAL is enabled once in initialize(); do not renegotiate it per request.
    connection.execute("PRAGMA busy_timeout=30000")
    return connection


_initialized = False


def initialize() -> None:
    """Initialize SQLite schema once per process, not once per historical date."""
    global _initialized
    if _initialized:
        return
    with _sync_lock:
        if _initialized:
            return
        with _connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
            CREATE TABLE IF NOT EXISTS daily_bars (
                symbol TEXT NOT NULL,
                trading_date TEXT NOT NULL,
                open REAL NOT NULL,
                high REAL NOT NULL,
                low REAL NOT NULL,
                close REAL NOT NULL,
                volume INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (symbol, trading_date)
            );
            CREATE INDEX IF NOT EXISTS idx_daily_bars_date ON daily_bars(trading_date);
            CREATE TABLE IF NOT EXISTS sync_dates (
                trading_date TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                rows_written INTEGER NOT NULL DEFAULT 0,
                detail TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sync_state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS index_sync_dates (
                trading_date TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                rows_written INTEGER NOT NULL DEFAULT 0,
                detail TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL
            );
            """)
            initialize_corporate_actions(db)
            # Reset empty markers created by the first five-year rollout, which
            # used the modern URL for pre-UDiFF dates. Do this exactly once.
            marker = db.execute(
                "SELECT value FROM sync_state WHERE key='legacy_bhavcopy_url_v1'"
            ).fetchone()
            if marker is None:
                db.execute(
                    "DELETE FROM sync_dates WHERE status='empty' AND trading_date < ?",
                    (UDIFF_CUTOVER.isoformat(),),
                )
                db.execute(
                    "INSERT INTO sync_state(key,value) VALUES('legacy_bhavcopy_url_v1','1')"
                )
        _initialized = True


def _clean_symbol(value: str):
    symbol = (value or "").strip().upper()
    for prefix in ("NSE:", "BSE:"):
        if symbol.startswith(prefix):
            symbol = symbol[len(prefix):]
    if symbol.endswith(("-EQ", "-BE")):
        symbol = symbol.rsplit("-", 1)[0]
    if symbol.endswith(".NS"):
        symbol = symbol[:-3]
    if symbol.endswith(".BO"):
        symbol = symbol[:-3]
    compact = "".join(ch for ch in symbol if ch.isalnum())
    aliases = {
        "NIFTY": "NIFTY", "NIFTY50": "NIFTY",
        "NIFTYBANK": "BANKNIFTY", "BANKNIFTY": "BANKNIFTY",
        "NIFTYFINANCIALSERVICES": "FINNIFTY", "FINNIFTY": "FINNIFTY",
        "NIFTYNEXT50": "NIFTYNXT50", "NIFTYNXT50": "NIFTYNXT50",
        "NIFTYMIDCAPSELECT": "MIDCPNIFTY", "MIDCPNIFTY": "MIDCPNIFTY",
    }
    return aliases.get(compact, symbol)


def _number(row: dict, *keys: str, integer: bool = False):
    for key in keys:
        raw = row.get(key)
        if raw is None or str(raw).strip() in {"", "-", "NA", "null"}:
            continue
        try:
            value = float(str(raw).replace(",", "").strip())
            if value == value:
                return int(value) if integer else value
        except (TypeError, ValueError):
            continue
    return 0 if integer else None


def _parse_bhavcopy(payload: bytes, requested_date: date) -> list[tuple]:
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        csv_names = [name for name in archive.namelist() if name.lower().endswith(".csv")]
        if not csv_names:
            raise ValueError("NSE bhavcopy ZIP contains no CSV.")
        with archive.open(csv_names[0]) as raw:
            stream = io.TextIOWrapper(raw, encoding="utf-8-sig", newline="")
            rows = csv.DictReader(stream)
            parsed = []
            for row in rows:
                symbol = _clean_symbol(row.get("TckrSymb") or row.get("SYMBOL") or "")
                series = str(row.get("SctySrs") or row.get("SERIES") or "").strip().upper()
                if not symbol or series not in {"EQ", "BE"}:
                    continue
                open_price = _number(row, "OpnPric", "OPEN_PRICE", "OPEN")
                high = _number(row, "HghPric", "HIGH_PRICE", "HIGH")
                low = _number(row, "LwPric", "LOW_PRICE", "LOW")
                close = _number(row, "ClsPric", "CLOSE_PRICE", "CLOSE")
                volume = _number(row, "TtlTradgVol", "TtlTrfVol", "TOTTRDQTY", "NO_OF_SHRS", integer=True)
                if any(value is None or value <= 0 for value in (open_price, high, low, close)):
                    continue
                parsed.append((symbol, requested_date.isoformat(), open_price, high, low, close, volume or 0, series))
            # Prefer EQ over BE when the same symbol occurs in both series.
            preferred = {}
            for row in parsed:
                if row[0] not in preferred or row[7] == "EQ":
                    preferred[row[0]] = row
            return [row[:7] for row in preferred.values()]


def _fetch_date(session: requests.Session, trading_date: date) -> list[tuple]:
    if trading_date < UDIFF_CUTOVER:
        month = trading_date.strftime("%b").upper()
        url = LEGACY_BASE_URL.format(
            year=trading_date.strftime("%Y"), month=month, day=trading_date.strftime("%d")
        )
    else:
        url = BASE_URL.format(date=trading_date.strftime("%Y%m%d"))
    response = session.get(url, headers=HEADERS, timeout=20)
    if response.status_code == 404:
        return []
    if response.status_code == 429:
        time.sleep(2)
        response = session.get(url, headers=HEADERS, timeout=25)
    if response.status_code != 200:
        raise RuntimeError(f"NSE bhavcopy HTTP {response.status_code} for {trading_date.isoformat()}")
    if not response.content:
        return []
    return _parse_bhavcopy(response.content, trading_date)


def _index_symbol(name: str) -> str:
    normalized = " ".join((name or "").strip().upper().replace("&", " AND ").split())
    aliases = {
        "NIFTY 50": "NIFTY",
        "NIFTY BANK": "BANKNIFTY",
        "NIFTY FINANCIAL SERVICES": "FINNIFTY",
        "NIFTY NEXT 50": "NIFTYNXT50",
        "NIFTY MIDCAP SELECT": "MIDCPNIFTY",
        "NIFTY MIDCAP 50": "NIFTYMIDCAP50",
        "NIFTY IT": "NIFTYIT",
        "NIFTY AUTO": "NIFTYAUTO",
        "NIFTY PHARMA": "NIFTYPHARMA",
        "NIFTY FMCG": "NIFTYFMCG",
        "NIFTY METAL": "NIFTYMETAL",
        "NIFTY REALTY": "NIFTYREALTY",
        "NIFTY ENERGY": "NIFTYENERGY",
        "NIFTY PSU BANK": "NIFTYPSUBANK",
        "NIFTY PRIVATE BANK": "NIFTYPRIVATEBANK",
    }
    if normalized in aliases:
        return aliases[normalized]
    if "FUTURES" in normalized or "STRATEGY" in normalized or "TOTAL RETURNS" in normalized:
        return ""
    return "".join(ch for ch in normalized if ch.isalnum())


def _parse_index_csv(payload: bytes, requested_date: date) -> list[tuple]:
    stream = io.TextIOWrapper(io.BytesIO(payload), encoding="utf-8-sig", newline="")
    reader = csv.DictReader(stream)
    parsed = []
    for raw in reader:
        row = {(key or "").strip(): value for key, value in raw.items()}
        symbol = _index_symbol(row.get("Index Name", ""))
        if not symbol:
            continue
        open_price = _number(row, "Open Index Value", "Open")
        high = _number(row, "High Index Value", "High")
        low = _number(row, "Low Index Value", "Low")
        close = _number(row, "Closing Index Value", "Closing", "Close")
        volume = _number(row, "Volume", integer=True)
        if any(value is None or value <= 0 for value in (open_price, high, low, close)):
            continue
        raw_date = str(row.get("Index Date") or "").strip()
        try:
            day = datetime.strptime(raw_date, "%d-%m-%Y").date()
        except ValueError:
            day = requested_date
        parsed.append((symbol, day.isoformat(), open_price, high, low, close, volume or 0))
    unique = {}
    for row in parsed:
        unique.setdefault(row[0], row)
    return list(unique.values())


def _fetch_index_date(session: requests.Session, trading_date: date) -> list[tuple]:
    url = INDEX_BASE_URL.format(date=trading_date.strftime("%d%m%Y"))
    response = session.get(url, headers=HEADERS, timeout=25)
    if response.status_code == 404:
        return []
    if response.status_code == 429 or response.status_code >= 500:
        time.sleep(1.5)
        response = session.get(url, headers=HEADERS, timeout=30)
    if response.status_code != 200:
        raise RuntimeError(f"NSE index archive HTTP {response.status_code} for {trading_date.isoformat()}")
    if len(response.content) < 500:
        raise RuntimeError(f"NSE index archive returned an unexpectedly small response for {trading_date.isoformat()}")
    return _parse_index_csv(response.content, trading_date)


def _index_already_processed(day: date, *, retry_empty: bool = False) -> bool:
    with _connect() as db:
        row = db.execute("SELECT status FROM index_sync_dates WHERE trading_date=?", (day.isoformat(),)).fetchone()
        if not row:
            return False
        return row["status"] == "ok" or (row["status"] == "empty" and not retry_empty)


def sync_index_day(day: date, session: requests.Session | None = None) -> int:
    initialize()
    own_session = session is None
    session = session or requests.Session()
    try:
        rows = _fetch_index_date(session, day)
        now = datetime.now(IST).isoformat()
        with _connect() as db:
            if rows:
                db.executemany("""
                    INSERT INTO daily_bars(symbol,trading_date,open,high,low,close,volume)
                    VALUES(?,?,?,?,?,?,?)
                    ON CONFLICT(symbol,trading_date) DO UPDATE SET
                      open=excluded.open, high=excluded.high, low=excluded.low,
                      close=excluded.close, volume=excluded.volume
                """, rows)
            db.execute("""
                INSERT INTO index_sync_dates(trading_date,status,rows_written,detail,updated_at)
                VALUES(?,?,?,?,?)
                ON CONFLICT(trading_date) DO UPDATE SET status=excluded.status,
                  rows_written=excluded.rows_written,detail=excluded.detail,updated_at=excluded.updated_at
            """, (day.isoformat(), "ok" if rows else "empty", len(rows), "", now))
        return len(rows)
    except Exception as exc:
        with _connect() as db:
            db.execute("""
                INSERT INTO index_sync_dates(trading_date,status,rows_written,detail,updated_at)
                VALUES(?,?,?,?,?)
                ON CONFLICT(trading_date) DO UPDATE SET status='error',detail=excluded.detail,updated_at=excluded.updated_at
            """, (day.isoformat(), "error", 0, str(exc)[:500], datetime.now(IST).isoformat()))
        raise
    finally:
        if own_session:
            session.close()


def sync_indices_recent(days: int = 10) -> dict:
    initialize()
    results = {"dates_checked": 0, "rows_written": 0, "errors": []}
    session = requests.Session()
    try:
        today = datetime.now(IST).date()
        for offset in range(days - 1, -1, -1):
            day = today - timedelta(days=offset)
            if day.weekday() >= 5 or _index_already_processed(day, retry_empty=True):
                continue
            results["dates_checked"] += 1
            try:
                results["rows_written"] += sync_index_day(day, session)
            except Exception as exc:
                results["errors"].append(f"{day.isoformat()}: {exc}")
            time.sleep(0.1)
    finally:
        session.close()
    return results


def backfill_indices(days: int = HISTORY_DAYS) -> dict:
    initialize()
    results = {"dates_checked": 0, "rows_written": 0, "errors": []}
    session = requests.Session()
    today = datetime.now(IST).date()
    start = today - timedelta(days=max(1, days) - 1)
    try:
        for offset in range((today - start).days + 1):
            day = start + timedelta(days=offset)
            if day.weekday() >= 5 or _index_already_processed(day):
                continue
            results["dates_checked"] += 1
            try:
                results["rows_written"] += sync_index_day(day, session)
            except Exception as exc:
                results["errors"].append(f"{day.isoformat()}: {exc}")
            time.sleep(0.1)
    finally:
        session.close()
    return results


def _already_processed(day: date, *, retry_empty: bool = False) -> bool:
    with _connect() as db:
        row = db.execute("SELECT status FROM sync_dates WHERE trading_date=?", (day.isoformat(),)).fetchone()
        if not row:
            return False
        return row["status"] == "ok" or (row["status"] == "empty" and not retry_empty)


def sync_day(day: date, session: requests.Session | None = None) -> int:
    initialize()
    own_session = session is None
    session = session or requests.Session()
    try:
        rows = _fetch_date(session, day)
        now = datetime.now(IST).isoformat()
        with _connect() as db:
            if rows:
                db.executemany("""
                    INSERT INTO daily_bars(symbol,trading_date,open,high,low,close,volume)
                    VALUES(?,?,?,?,?,?,?)
                    ON CONFLICT(symbol,trading_date) DO UPDATE SET
                      open=excluded.open, high=excluded.high, low=excluded.low,
                      close=excluded.close, volume=excluded.volume
                """, rows)
            db.execute("""
                INSERT INTO sync_dates(trading_date,status,rows_written,detail,updated_at)
                VALUES(?,?,?,?,?)
                ON CONFLICT(trading_date) DO UPDATE SET
                  status=excluded.status, rows_written=excluded.rows_written,
                  detail=excluded.detail, updated_at=excluded.updated_at
            """, (day.isoformat(), "ok" if rows else "empty", len(rows), "", now))
            db.execute("INSERT INTO sync_state(key,value) VALUES('last_attempt',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (now,))
            if rows:
                db.execute("INSERT INTO sync_state(key,value) VALUES('last_data_date',?) ON CONFLICT(key) DO UPDATE SET value=CASE WHEN excluded.value > sync_state.value THEN excluded.value ELSE sync_state.value END", (day.isoformat(),))
        return len(rows)
    except Exception as exc:
        with _connect() as db:
            db.execute("""
                INSERT INTO sync_dates(trading_date,status,rows_written,detail,updated_at)
                VALUES(?,?,?,?,?)
                ON CONFLICT(trading_date) DO UPDATE SET status='error',detail=excluded.detail,updated_at=excluded.updated_at
            """, (day.isoformat(), "error", 0, str(exc)[:500], datetime.now(IST).isoformat()))
        raise
    finally:
        if own_session:
            session.close()


def sync_recent(days: int = 10) -> dict:
    """Update recent sessions first so the chart starts working before backfill finishes."""
    initialize()
    results = {"dates_checked": 0, "rows_written": 0, "errors": []}
    session = requests.Session()
    try:
        today = datetime.now(IST).date()
        for offset in range(days - 1, -1, -1):
            day = today - timedelta(days=offset)
            if day.weekday() >= 5 or _already_processed(day, retry_empty=True):
                continue
            results["dates_checked"] += 1
            try:
                results["rows_written"] += sync_day(day, session)
            except Exception as exc:
                results["errors"].append(f"{day.isoformat()}: {exc}")
            time.sleep(0.15)
    finally:
        session.close()
    return results


def backfill(days: int = HISTORY_DAYS) -> dict:
    """Resumable multi-year daily backfill. Successful dates are never fetched twice."""
    initialize()
    results = {"dates_checked": 0, "rows_written": 0, "errors": []}
    session = requests.Session()
    today = datetime.now(IST).date()
    start = today - timedelta(days=max(1, days) - 1)
    try:
        for index in range((today - start).days + 1):
            day = start + timedelta(days=index)
            if day.weekday() >= 5 or _already_processed(day):
                continue
            results["dates_checked"] += 1
            try:
                results["rows_written"] += sync_day(day, session)
            except Exception as exc:
                results["errors"].append(f"{day.isoformat()}: {exc}")
            time.sleep(0.15)
    finally:
        session.close()
    # Do not prune older rows: the database is intentionally a rolling five-year-plus
    # archive. Backfill only inserts missing dates, so expanding history is resumable.
    return results


def sync_corporate_actions(*, historical: bool = False) -> dict:
    """Refresh the official NSE action feed and persist auditable sync status."""
    initialize()
    today = datetime.now(IST).date()
    start = today - timedelta(days=HISTORY_DAYS if historical else 90)
    end = today + timedelta(days=180)
    with _connect() as db:
        result = refresh_nse_corporate_actions(db, start_date=start, end_date=end)
        now = datetime.now(IST).isoformat()
        db.execute(
            "INSERT INTO sync_state(key,value) VALUES('corporate_actions_last_run',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (now,),
        )
        db.execute(
            "INSERT INTO sync_state(key,value) VALUES('corporate_actions_last_status',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (result["status"],),
        )
        db.execute(
            "INSERT INTO sync_state(key,value) VALUES('corporate_actions_last_error',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            ("; ".join(result["errors"][:10]),),
        )
        if historical and result["status"] == "ok":
            db.execute(
                "INSERT INTO sync_state(key,value) VALUES('corporate_actions_historical_status','complete') "
                "ON CONFLICT(key) DO UPDATE SET value='complete'"
            )
        elif historical:
            db.execute(
                "INSERT INTO sync_state(key,value) VALUES('corporate_actions_historical_status','partial') "
                "ON CONFLICT(key) DO UPDATE SET value='partial'"
            )
        db.commit()
    return result


def start_background_sync() -> None:
    """Refresh recent data first, then resumably backfill five years in the background."""
    def worker():
        initialize()
        sync_recent(10)
        sync_indices_recent(10)
        backfill(HISTORY_DAYS)
        backfill_indices(HISTORY_DAYS)
        # Retry the historical import on subsequent starts until every bounded
        # NSE date window succeeds; this avoids silently accepting partial coverage.
        with _connect() as db:
            historical_state = db.execute(
                "SELECT value FROM sync_state WHERE key='corporate_actions_historical_status'"
            ).fetchone()
        sync_corporate_actions(historical=historical_state is None or historical_state["value"] != "complete")
        while True:
            now = datetime.now(IST)
            target = now.replace(hour=16, minute=10, second=0, microsecond=0)
            if now >= target:
                target += timedelta(days=1)
            time.sleep(max(30, (target - now).total_seconds()))
            # Run after the cash market close; skip weekend days.
            if datetime.now(IST).weekday() < 5:
                sync_recent(10)
                sync_indices_recent(10)
                sync_corporate_actions(historical=False)
                # Keep the five-year history; only recent dates are refreshed daily.
    thread = threading.Thread(target=worker, name="pipsgox-sqlite-eod-sync", daemon=True)
    thread.start()


class SQLiteMarketDataProvider:
    name = "sqlite"

    def get_history(self, symbol: str, timeframe: str, limit: int, *, start=None, end=None, adjustment: str = "raw") -> list[Candle]:
        if timeframe not in {"D", "W", "M"}:
            raise ValueError("Local SQLite database contains daily EOD candles only. Intraday timeframes require intraday data.")
        clean = _clean_symbol(symbol)
        params: list = [clean]
        query = "SELECT trading_date,open,high,low,close,volume FROM daily_bars WHERE symbol=?"
        if start is not None:
            query += " AND trading_date >= ?"
            params.append(start.isoformat())
        if end is not None:
            query += " AND trading_date <= ?"
            params.append(end.isoformat())
        query += " ORDER BY trading_date DESC LIMIT ?"
        params.append(max(1, min(int(limit), 2000)))
        with _connect() as db:
            rows = list(db.execute(query, params))
        rows.reverse()
        candles = [Candle(
            time=int(datetime.combine(date.fromisoformat(row["trading_date"]), datetime.min.time(), tzinfo=IST).timestamp()),
            open=float(row["open"]), high=float(row["high"]), low=float(row["low"]),
            close=float(row["close"]), volume=int(row["volume"] or 0)
        ) for row in rows]
        with _connect() as db:\n            candles = adjust_candles(db, clean, candles, adjustment)\n        if timeframe in {"W", "M"}:
            grouped = {}
            for candle, row in zip(candles, rows):
                day = date.fromisoformat(row["trading_date"])
                key = day.isocalendar()[:2] if timeframe == "W" else (day.year, day.month)
                if key not in grouped:
                    grouped[key] = [candle.time, candle.open, candle.high, candle.low, candle.close, candle.volume]
                else:
                    item = grouped[key]
                    item[2] = max(item[2], candle.high)
                    item[3] = min(item[3], candle.low)
                    item[4] = candle.close
                    item[5] += candle.volume
                    item[0] = candle.time
            candles = [Candle(time=v[0], open=v[1], high=v[2], low=v[3], close=v[4], volume=v[5]) for v in grouped.values()]
            candles = candles[-limit:]
        if not candles:
            raise ValueError(f"No SQLite EOD data for {clean}. The initial NSE backfill may still be running.")
        return candles

    def get_history_batch(self, symbols: list[str], timeframe: str, limit: int, **kwargs) -> dict[str, list[Candle]]:
        result = {}
        for symbol in symbols:
            try:
                result[_clean_symbol(symbol)] = self.get_history(symbol, timeframe, limit, **kwargs)
            except (ValueError, sqlite3.Error):
                continue
        return result

    def get_quote(self, symbol: str) -> Quote:
        candles = self.get_history(symbol, "D", 2)
        last = candles[-1]
        previous = candles[-2] if len(candles) > 1 else last
        change = last.close - previous.close
        return Quote(
            symbol=_clean_symbol(symbol), exchange="NSE", last=last.close,
            change=change, change_percent=(change / previous.close * 100.0) if previous.close else 0.0,
            open=last.open, high=last.high, low=last.low, volume=last.volume
        )

    def get_quotes(self, symbols: list[str]) -> list[Quote]:
        cleaned = list(dict.fromkeys(_clean_symbol(symbol) for symbol in symbols if _clean_symbol(symbol)))
        if not cleaned:
            return []
        placeholders = ",".join("?" for _ in cleaned)
        with _connect() as db:
            rows = list(db.execute(f"""
                SELECT symbol,trading_date,open,high,low,close,volume FROM (
                    SELECT symbol,trading_date,open,high,low,close,volume,
                           ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY trading_date DESC) AS rn
                    FROM daily_bars WHERE symbol IN ({placeholders})
                ) WHERE rn <= 2 ORDER BY symbol,trading_date
            """, cleaned))
        grouped = {}
        for row in rows:
            grouped.setdefault(row["symbol"], []).append(row)
        quotes = []
        for symbol in cleaned:
            data = grouped.get(symbol, [])
            if not data:
                continue
            previous = data[-2] if len(data) > 1 else data[-1]
            last = data[-1]
            change = float(last["close"]) - float(previous["close"])
            prev_close = float(previous["close"])
            quotes.append(Quote(
                symbol=symbol, exchange="NSE", last=float(last["close"]), change=change,
                change_percent=(change / prev_close * 100.0) if prev_close else 0.0,
                open=float(last["open"]), high=float(last["high"]), low=float(last["low"]),
                volume=int(last["volume"] or 0)
            ))
        return quotes



def symbol_history_status(symbol: str) -> dict:
    """Return per-symbol candle coverage and unusually large calendar gaps."""
    clean = _clean_symbol(symbol)
    initialize()
    with _connect() as db:
        rows = list(db.execute(
            "SELECT trading_date FROM daily_bars WHERE symbol=? ORDER BY trading_date",
            (clean,),
        ))
    dates = [date.fromisoformat(row["trading_date"]) for row in rows]
    gaps = []
    for previous, current in zip(dates, dates[1:]):
        missing_calendar_days = (current - previous).days
        # A normal Friday-to-Monday interval is 3 days. Flag gaps over 7 days.
        if missing_calendar_days > 7:
            gaps.append({
                "after": previous.isoformat(),
                "before": current.isoformat(),
                "calendar_days": missing_calendar_days,
            })
    return {
        "symbol": clean,
        "bars": len(dates),
        "first_date": dates[0].isoformat() if dates else None,
        "last_date": dates[-1].isoformat() if dates else None,
        "large_gap_count": len(gaps),
        "largest_gaps": sorted(gaps, key=lambda item: item["calendar_days"], reverse=True)[:10],
    }


def sync_status() -> dict:
    initialize()
    with _connect() as db:
        initialize_corporate_actions(db)\n        corporate = action_status(db)\n        count = db.execute("SELECT COUNT(*) FROM daily_bars").fetchone()[0]
        symbols = db.execute("SELECT COUNT(DISTINCT symbol) FROM daily_bars").fetchone()[0]
        first = db.execute("SELECT MIN(trading_date) FROM daily_bars").fetchone()[0]
        last = db.execute("SELECT MAX(trading_date) FROM daily_bars").fetchone()[0]
        state = {row["key"]: row["value"] for row in db.execute("SELECT key,value FROM sync_state")}
        errors = [dict(row) for row in db.execute("SELECT trading_date,detail FROM sync_dates WHERE status='error' ORDER BY trading_date DESC LIMIT 5")]
        index_count = db.execute("SELECT COUNT(*) FROM daily_bars WHERE symbol IN ('NIFTY','BANKNIFTY','FINNIFTY','NIFTYNXT50','MIDCPNIFTY','NIFTYMIDCAP50','NIFTYIT','NIFTYAUTO','NIFTYPHARMA','NIFTYFMCG','NIFTYMETAL','NIFTYREALTY','NIFTYENERGY','NIFTYPSUBANK','NIFTYPRIVATEBANK')").fetchone()[0]
        index_symbols = db.execute("SELECT COUNT(DISTINCT symbol) FROM daily_bars WHERE symbol IN ('NIFTY','BANKNIFTY','FINNIFTY','NIFTYNXT50','MIDCPNIFTY','NIFTYMIDCAP50','NIFTYIT','NIFTYAUTO','NIFTYPHARMA','NIFTYFMCG','NIFTYMETAL','NIFTYREALTY','NIFTYENERGY','NIFTYPSUBANK','NIFTYPRIVATEBANK')").fetchone()[0]
        index_errors = [dict(row) for row in db.execute("SELECT trading_date,detail FROM index_sync_dates WHERE status='error' ORDER BY trading_date DESC LIMIT 5")]
    return {"database": str(_db_path()), "corporate_actions": corporate, "bars": count, "symbols": symbols, "first_date": first, "last_date": last, "index_bars": index_count, "index_symbols": index_symbols, "index_recent_errors": index_errors, **state, "recent_errors": errors}


if __name__ == "__main__":
    import argparse
    import json

    parser = argparse.ArgumentParser(description="Seed or refresh the local SQLite NSE EOD database.")
    parser.add_argument("--backfill-days", type=int, default=HISTORY_DAYS, help="Calendar days to backfill (default: five years).")
    parser.add_argument("--recent-only", action="store_true", help="Only refresh the latest ten calendar days.")
    parser.add_argument("--status", action="store_true", help="Print local database coverage and exit.")
    args = parser.parse_args()
    initialize()
    if args.status:
        print(json.dumps(sync_status(), indent=2))
    elif args.recent_only:
        print(json.dumps(sync_recent(10), indent=2))
    else:
        print(json.dumps(sync_recent(10), indent=2))
        print(json.dumps(backfill(args.backfill_days), indent=2))
        print(json.dumps(backfill_indices(args.backfill_days), indent=2))
    print(json.dumps(sync_status(), indent=2))
