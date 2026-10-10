from __future__ import annotations

"""Broker-independent market data backed by the local SQLite EOD database."""

import threading
from dataclasses import dataclass
from typing import Literal

from app.providers.base import Candle, Quote
from app.sqlite_market_data import SQLiteMarketDataProvider

MarketDataSource = Literal["sqlite"]
SOURCE_NAMES = ("sqlite",)


def configured_source() -> MarketDataSource:
    # Deliberately fixed: market data is sourced only from our local SQLite DB.
    return "sqlite"


@dataclass(frozen=True)
class MarketDataHealth:
    source: str = "sqlite"
    configured: bool = True
    lazy: bool = True


class MarketDataMaster:
    """Single-provider registry; SQLite is the only market-data source."""

    def __init__(self) -> None:
        self._provider = None
        self._lock = threading.Lock()

    def source(self) -> MarketDataSource:
        return "sqlite"

    def health(self) -> MarketDataHealth:
        return MarketDataHealth()

    def provider(self) -> SQLiteMarketDataProvider:
        with self._lock:
            if self._provider is None:
                self._provider = SQLiteMarketDataProvider()
            return self._provider

    def get_history(self, *args, **kwargs) -> list[Candle]:
        return self.provider().get_history(*args, **kwargs)

    def get_history_batch(self, symbols: list[str], timeframe: str, limit: int, **kwargs) -> dict[str, list[Candle]]:
        return self.provider().get_history_batch(symbols, timeframe, limit, **kwargs)

    def get_quote(self, *args, **kwargs) -> Quote:
        return self.provider().get_quote(*args, **kwargs)

    def get_quotes(self, symbols: list[str]) -> list[Quote]:
        return self.provider().get_quotes(symbols)


market_data_master = MarketDataMaster()
