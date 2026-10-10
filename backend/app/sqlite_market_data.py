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

IST = timezone(timedelta(hours=5, minutes=30))
BASE_URL = "https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_{date}_F_0000.csv.zip"
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


def initialize() -> None:
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


def _clean_symbol(value: str) -> str:
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
                symbol = _clean_symbol(row.get("TckrSymb", ""))
                series = str(row.get("SctySrs") or "").strip().upper()
                if not symbol or series not in {"EQ", "BE"}:
                    continue
                open_price = _number(row, "OpnPric", "OPEN_PRICE")
                high = _number(row, "HghPric", "HIGH_PRICE")
                low = _number(row, "LwPric", "LOW_PRICE")
                close = _number(row, "ClsPric", "CLOSE_PRICE")
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


def backfill(days: int = 365) -> dict:
    """Resumable one-year daily backfill. Successful dates are never fetched twice."""
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
    cutoff = (today - timedelta(days=365)).isoformat()
    with _connect() as db:
        db.execute("DELETE FROM daily_bars WHERE trading_date < ?", (cutoff,))
    return results


def start_background_sync() -> None:
    """Start one worker: refresh recent data, then fill the remaining year, then daily updates."""
    def worker():
        initialize()
        sync_recent(10)
        backfill(365)
        while True:
            now = datetime.now(IST)
            target = now.replace(hour=16, minute=10, second=0, microsecond=0)
            if now >= target:
                target += timedelta(days=1)
            time.sleep(max(30, (target - now).total_seconds()))
            # Run after the cash market close; skip weekend days.
            if datetime.now(IST).weekday() < 5:
                sync_recent(10)
                cutoff = (datetime.now(IST).date() - timedelta(days=365)).isoformat()
                with _connect() as db:
                    db.execute("DELETE FROM daily_bars WHERE trading_date < ?", (cutoff,))
    thread = threading.Thread(target=worker, name="pipsgox-sqlite-eod-sync", daemon=True)
    thread.start()


class SQLiteMarketDataProvider:
    name = "sqlite"

    def get_history(self, symbol: str, timeframe: str, limit: int, *, start=None, end=None) -> list[Candle]:
        if timeframe not in {"D", "W", "M"}:
            raise ValueError("Local SQLite database contains daily EOD candles only. Intraday timeframes require intraday data.")
        clean = _clean_symbol(symbol)
        initialize()
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
        if timeframe in {"W", "M"}:
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


def sync_status() -> dict:
    initialize()
    with _connect() as db:
        count = db.execute("SELECT COUNT(*) FROM daily_bars").fetchone()[0]
        symbols = db.execute("SELECT COUNT(DISTINCT symbol) FROM daily_bars").fetchone()[0]
        first = db.execute("SELECT MIN(trading_date) FROM daily_bars").fetchone()[0]
        last = db.execute("SELECT MAX(trading_date) FROM daily_bars").fetchone()[0]
        state = {row["key"]: row["value"] for row in db.execute("SELECT key,value FROM sync_state")}
        errors = [dict(row) for row in db.execute("SELECT trading_date,detail FROM sync_dates WHERE status='error' ORDER BY trading_date DESC LIMIT 5")]
    return {"database": str(_db_path()), "bars": count, "symbols": symbols, "first_date": first, "last_date": last, **state, "recent_errors": errors}


if __name__ == "__main__":
    import argparse
    import json

    parser = argparse.ArgumentParser(description="Seed or refresh the local SQLite NSE EOD database.")
    parser.add_argument("--backfill-days", type=int, default=365, help="Calendar days to backfill (default: 365).")
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
    print(json.dumps(sync_status(), indent=2))
