from __future__ import annotations

"""Lightweight broker-independent market-data master.

The terminal uses this layer for market data, while broker APIs remain
responsible for trading.  Providers are imported lazily so selecting a source
does not add startup work or a persistent loading state.
"""

import os
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Literal

from app.providers.base import Candle, Quote

MarketDataSource = Literal["yfinance", "nse", "bse"]

SOURCE_NAMES = ("yfinance", "nse", "bse")


def configured_source() -> MarketDataSource:
    value = os.getenv("PIPSGOX_MARKET_DATA_SOURCE", "yfinance").strip().lower()
    if value not in SOURCE_NAMES:
        return "yfinance"
    return value  # type: ignore[return-value]


def _symbol_for_yfinance(symbol: str) -> str:
    clean = symbol.strip().upper()
    if clean.startswith("NSE:"):
        clean = clean[4:]
    if clean.startswith("BSE:"):
        clean = clean[4:]
        if clean.endswith(".BO"):
            return clean
        if clean.isdigit():
            return f"{clean}.BO"
        return f"{clean}.BO"
    if clean.endswith("-EQ") or clean.endswith("-BE"):
        clean = clean.rsplit("-", 1)[0]
    if clean.endswith(".NS") or clean.endswith(".BO"):
        return clean
    return f"{clean}.NS"


def _timestamp(value) -> int:
    if hasattr(value, "to_pydatetime"):
        value = value.to_pydatetime()
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=datetime.now().astimezone().tzinfo)
        return int(value.timestamp())
    return int(value)


def _frame_from_row(row) -> Candle:
    return Candle(
        time=_timestamp(row.name),
        open=float(row["Open"]),
        high=float(row["High"]),
        low=float(row["Low"]),
        close=float(row["Close"]),
        volume=int(row.get("Volume", 0) or 0),
    )


@dataclass(frozen=True)
class MarketDataHealth:
    source: str
    configured: bool = True
    lazy: bool = True


class _YFinanceProvider:
    name = "yfinance"

    def _ticker(self, symbol: str):
        import yfinance as yf
        # Keep Yahoo requests quiet and bounded. Recent yfinance versions expose
        # network retries globally; guard this so older installed versions still work.
        try:
            yf.config.network.retries = 2
            yf.config.debug.hide_exceptions = False
        except AttributeError:
            pass
        return yf.Ticker(_symbol_for_yfinance(symbol))

    def get_history(self, symbol: str, timeframe: str, limit: int, *, start=None, end=None) -> list[Candle]:
        ticker = self._ticker(symbol)
        interval_map = {"1m": "1m", "3m": "5m", "5m": "5m", "15m": "15m", "30m": "30m", "1h": "60m", "D": "1d", "W": "1wk", "M": "1mo"}
        interval = interval_map.get(timeframe)
        if interval is None:
            raise ValueError(f"Unsupported timeframe: {timeframe}")

        kwargs = {
            "interval": interval,
            "auto_adjust": False,
            "actions": False,
            "progress": False,
            "timeout": 20,
            "raise_errors": True,
            "repair": False,
        }
        if start or end:
            kwargs["start"] = start.isoformat() if start else None
            kwargs["end"] = (end + timedelta(days=1)).isoformat() if end else None
        else:
            period_map = {"1m": "7d", "3m": "60d", "5m": "60d", "15m": "60d", "30m": "60d", "1h": "730d", "D": "10y", "W": "10y", "M": "20y"}
            kwargs["period"] = period_map.get(timeframe, "10y")

        frame = ticker.history(**kwargs)
        if frame is None or frame.empty:
            raise ValueError(f"No historical data returned for {symbol.strip().upper()} from yfinance.")
        if hasattr(frame.columns, "levels"):
            frame.columns = frame.columns.get_level_values(0)
        return [_frame_from_row(row) for _, row in frame.tail(limit).iterrows()]

    def get_quotes(self, symbols: list[str]) -> list[Quote]:
        """Fetch multiple quotes in one Yahoo request to keep scans lightweight."""
        requested = [item.strip().upper() for item in symbols if item and item.strip()]
        if not requested:
            return []

        import yfinance as yf

        yahoo_symbols = [_symbol_for_yfinance(symbol) for symbol in requested]
        try:
            yf.config.network.retries = 2
            yf.config.debug.hide_exceptions = False
        except AttributeError:
            pass

        frame = yf.download(
            tickers=yahoo_symbols,
            period="5d",
            interval="1d",
            group_by="ticker",
            auto_adjust=False,
            actions=False,
            progress=False,
            threads=False,
            timeout=20,
            repair=False,
            multi_level_index=True,
        )
        if frame is None or frame.empty:
            raise ValueError("No quote data returned for the requested symbols from yfinance.")

        results: list[Quote] = []
        for original, yahoo_symbol in zip(requested, yahoo_symbols):
            try:
                if hasattr(frame.columns, "levels"):
                    if yahoo_symbol in frame.columns.get_level_values(0):
                        rows = frame[yahoo_symbol]
                    elif yahoo_symbol in frame.columns.get_level_values(1):
                        rows = frame.xs(yahoo_symbol, axis=1, level=1)
                    else:
                        continue
                else:
                    rows = frame

                rows = rows.dropna(subset=["Close"])
                if rows.empty:
                    continue
                last = rows.iloc[-1]
                previous = rows.iloc[-2] if len(rows) > 1 else last
                close = float(last["Close"])
                prev_close = float(previous["Close"])
                change = close - prev_close
                results.append(
                    Quote(
                        symbol=original,
                        exchange="BSE" if yahoo_symbol.endswith(".BO") else "NSE",
                        last=close,
                        change=change,
                        change_percent=(change / prev_close * 100.0) if prev_close else 0.0,
                        open=float(last["Open"]),
                        high=float(last["High"]),
                        low=float(last["Low"]),
                        volume=int(last.get("Volume", 0) or 0),
                    )
                )
            except (KeyError, TypeError, ValueError):
                continue

        return results

    def get_quote(self, symbol: str) -> Quote:
        clean = symbol.strip().upper()
        ticker = self._ticker(clean)
        frame = ticker.history(
            period="5d",
            interval="1d",
            auto_adjust=False,
            actions=False,
            progress=False,
            timeout=20,
            raise_errors=True,
            repair=False,
        )
        if frame is None or frame.empty:
            raise ValueError(f"No quote returned for {clean} from yfinance.")
        if hasattr(frame.columns, "levels"):
            frame.columns = frame.columns.get_level_values(0)
        last = frame.iloc[-1]
        previous = frame.iloc[-2] if len(frame) > 1 else None
        close = float(last["Close"])
        prev_close = float(previous["Close"]) if previous is not None else close
        change = close - prev_close
        return Quote(
            symbol=clean,
            exchange="BSE" if _symbol_for_yfinance(clean).endswith(".BO") else "NSE",
            last=close,
            change=change,
            change_percent=(change / prev_close * 100.0) if prev_close else 0.0,
            open=float(last["Open"]),
            high=float(last["High"]),
            low=float(last["Low"]),
            volume=int(last.get("Volume", 0) or 0),
        )


class _NseProvider:
    name = "nse"

    @staticmethod
    def _ticker(symbol: str):
        try:
            import nsefeed
        except ImportError as exc:
            raise RuntimeError("NSE data source is not installed. Install the nsefeed package.") from exc
        clean = symbol.strip().upper().replace("NSE:", "")
        clean = clean.rsplit("-", 1)[0] if clean.endswith(("-EQ", "-BE")) else clean
        return nsefeed.Ticker(clean)

    def get_history(self, symbol: str, timeframe: str, limit: int, *, start=None, end=None) -> list[Candle]:
        if timeframe not in {"D", "W", "M"}:
            raise ValueError("The NSE source currently provides daily historical data; use yfinance for intraday intervals.")
        ticker = self._ticker(symbol)
        period = "10Y" if not start else None
        frame = ticker.history(period=period, start=start.isoformat() if start else None, end=(end + timedelta(days=1)).isoformat() if end else None)
        if frame is None or frame.empty:
            raise ValueError(f"No historical data returned for {symbol.strip().upper()} from NSE.")
        if timeframe == "W":
            frame = frame.resample("W").agg({"Open":"first","High":"max","Low":"min","Close":"last","Volume":"sum"}).dropna()
        elif timeframe == "M":
            frame = frame.resample("ME").agg({"Open":"first","High":"max","Low":"min","Close":"last","Volume":"sum"}).dropna()
        return [_frame_from_row(row) for _, row in frame.tail(limit).iterrows()]

    def get_quote(self, symbol: str) -> Quote:
        candles = self.get_history(symbol, "D", 2)
        last = candles[-1]
        previous = candles[-2] if len(candles) > 1 else last
        change = last.close - previous.close
        return Quote(symbol=symbol.strip().upper(), exchange="NSE", last=last.close, change=change, change_percent=(change / previous.close * 100.0) if previous.close else 0.0, open=last.open, high=last.high, low=last.low, volume=last.volume)


class _BseProvider:
    name = "bse"

    @staticmethod
    def _history(symbol: str, start=None, end=None):
        try:
            from bseindia import equity
        except ImportError as exc:
            raise RuntimeError("BSE data source is not installed. Install the bseindia package.") from exc
        clean = symbol.strip().upper().replace("BSE:", "")
        return equity.historical_stock_data(
            symbol=clean,
            from_date=start.strftime("%d-%m-%Y") if start else None,
            to_date=end.strftime("%d-%m-%Y") if end else None,
            period=None if start else "10Y",
        )

    def get_history(self, symbol: str, timeframe: str, limit: int, *, start=None, end=None) -> list[Candle]:
        if timeframe not in {"D", "W", "M"}:
            raise ValueError("The BSE source currently provides daily historical data; use yfinance for intraday intervals.")
        frame = self._history(symbol, start, end)
        if frame is None or frame.empty:
            raise ValueError(f"No historical data returned for {symbol.strip().upper()} from BSE.")
        frame.columns = [str(c).strip().title() for c in frame.columns]
        aliases = {"Date":"Date","Open":"Open","High":"High","Low":"Low","Close":"Close","Volume":"Volume"}
        frame = frame.rename(columns=aliases)
        if "Date" in frame.columns:
            frame["Date"] = frame["Date"].apply(lambda x: datetime.fromisoformat(str(x)[:10]) if not isinstance(x, datetime) else x)
            frame = frame.set_index("Date")
        for col in ("Open","High","Low","Close","Volume"):
            if col not in frame.columns:
                raise ValueError(f"BSE historical data is missing {col}.")
        if timeframe == "W":
            frame = frame.resample("W").agg({"Open":"first","High":"max","Low":"min","Close":"last","Volume":"sum"}).dropna()
        elif timeframe == "M":
            frame = frame.resample("ME").agg({"Open":"first","High":"max","Low":"min","Close":"last","Volume":"sum"}).dropna()
        return [_frame_from_row(row) for _, row in frame.tail(limit).iterrows()]

    def get_quote(self, symbol: str) -> Quote:
        candles = self.get_history(symbol, "D", 2)
        last = candles[-1]
        previous = candles[-2] if len(candles) > 1 else last
        change = last.close - previous.close
        return Quote(symbol=symbol.strip().upper(), exchange="BSE", last=last.close, change=change, change_percent=(change / previous.close * 100.0) if previous.close else 0.0, open=last.open, high=last.high, low=last.low, volume=last.volume)


class MarketDataMaster:
    """Lazy source registry; no provider is constructed during application startup."""

    def __init__(self) -> None:
        self._providers = {}
        self._lock = threading.Lock()

    def source(self) -> MarketDataSource:
        return configured_source()

    def health(self) -> MarketDataHealth:
        return MarketDataHealth(source=self.source())

    def provider(self):
        source = self.source()
        with self._lock:
            provider = self._providers.get(source)
            if provider is not None:
                return provider
            provider = {"yfinance": _YFinanceProvider, "nse": _NseProvider, "bse": _BseProvider}[source]()
            self._providers[source] = provider
            return provider

    def get_history(self, *args, **kwargs):
        return self.provider().get_history(*args, **kwargs)

    def get_quote(self, *args, **kwargs):
        return self.provider().get_quote(*args, **kwargs)

    def get_quotes(self, symbols: list[str]) -> list[Quote]:
        provider = self.provider()
        batch_method = getattr(provider, "get_quotes", None)
        if callable(batch_method):
            return batch_method(symbols)
        return [provider.get_quote(symbol) for symbol in symbols]


market_data_master = MarketDataMaster()
