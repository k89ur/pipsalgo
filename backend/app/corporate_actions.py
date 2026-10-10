from __future__ import annotations

"""Verified corporate-action ledger and deterministic EOD adjustment engine.

Raw NSE daily_bars are never modified. Only verified split/bonus/consolidation
events with an explicit positive price factor are applied automatically.
Other event types remain recorded but unapplied until their event-specific
adjustment methodology has been verified.
"""
import sqlite3
from datetime import date, datetime, timedelta, timezone
from typing import Any

from app.providers.base import Candle


SUPPORTED_PRICE_FACTOR_TYPES = {"split", "bonus", "consolidation"}
IST = timezone(timedelta(hours=5, minutes=30))


def initialize_corporate_actions(db: sqlite3.Connection) -> None:
    db.executescript("""
    CREATE TABLE IF NOT EXISTS corporate_actions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        symbol TEXT NOT NULL,
        action_type TEXT NOT NULL,
        announcement_date TEXT,
        ex_date TEXT NOT NULL,
        record_date TEXT,
        purpose TEXT NOT NULL DEFAULT '',
        ratio_text TEXT NOT NULL DEFAULT '',
        price_factor REAL,
        volume_factor REAL,
        dividend_amount REAL,
        source_url TEXT NOT NULL DEFAULT '',
        source_name TEXT NOT NULL DEFAULT 'NSE',
        verification_status TEXT NOT NULL DEFAULT 'pending'
            CHECK (verification_status IN ('pending','verified','rejected')),
        adjustment_status TEXT NOT NULL DEFAULT 'not_applied'
            CHECK (adjustment_status IN ('not_applied','applied','manual_review')),
        notes TEXT NOT NULL DEFAULT '',
        source_payload TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(symbol, action_type, ex_date, purpose)
    );
    CREATE INDEX IF NOT EXISTS idx_corporate_actions_symbol_date
      ON corporate_actions(symbol, ex_date);
    CREATE INDEX IF NOT EXISTS idx_corporate_actions_status
      ON corporate_actions(verification_status, ex_date);
    """)


def validate_action(action: dict[str, Any]) -> dict[str, Any]:
    """Validate normalized event data before it is written to the ledger."""
    symbol = str(action.get("symbol") or "").strip().upper()
    action_type = str(action.get("action_type") or "").strip().lower()
    ex_date = str(action.get("ex_date") or "").strip()
    if not symbol or not action_type:
        raise ValueError("symbol and action_type are required")
    try:
        date.fromisoformat(ex_date)
    except ValueError as exc:
        raise ValueError("ex_date must be YYYY-MM-DD") from exc

    price_factor = action.get("price_factor")
    volume_factor = action.get("volume_factor")
    status = str(action.get("verification_status") or "pending").lower()
    if status not in {"pending", "verified", "rejected"}:
        raise ValueError("verification_status must be pending, verified, or rejected")
    if action_type in SUPPORTED_PRICE_FACTOR_TYPES and status == "verified":
        if price_factor is None or volume_factor is None:
            raise ValueError("Verified split/bonus/consolidation actions require explicit price and volume factors")
        if not (0 < float(price_factor) <= 1 and float(volume_factor) >= 1):
            raise ValueError("Price factor must be in (0, 1] and volume factor must be >= 1")
        if abs(float(price_factor) * float(volume_factor) - 1.0) > 0.03:
            raise ValueError("Price and volume factors must be approximately reciprocal")
    else:
        # Prevent unverified or unsupported event types from changing charts.
        if status != "verified" or action_type not in SUPPORTED_PRICE_FACTOR_TYPES:
            price_factor = None
            volume_factor = None

    normalized = dict(action)
    normalized.update({
        "symbol": symbol,
        "action_type": action_type,
        "ex_date": ex_date,
        "price_factor": float(price_factor) if price_factor is not None else None,
        "volume_factor": float(volume_factor) if volume_factor is not None else None,
        "verification_status": status,
    })
    return normalized


def record_action(db: sqlite3.Connection, action: dict[str, Any]) -> int:
    """Insert/update a normalized event. Caller must provide official source evidence."""
    item = validate_action(action)
    columns = (
        "symbol", "action_type", "announcement_date", "ex_date", "record_date",
        "purpose", "ratio_text", "price_factor", "volume_factor", "dividend_amount",
        "source_url", "source_name", "verification_status", "adjustment_status",
        "notes", "source_payload",
    )
    values = [item.get(column) for column in columns]
    placeholders = ",".join("?" for _ in columns)
    updates = ",".join(f"{column}=excluded.{column}" for column in columns if column not in {
        "symbol", "action_type", "ex_date", "purpose"
    })
    cursor = db.execute(
        f"INSERT INTO corporate_actions ({','.join(columns)}) VALUES ({placeholders}) "
        f"ON CONFLICT(symbol,action_type,ex_date,purpose) DO UPDATE SET {updates}, "
        "updated_at=CURRENT_TIMESTAMP",
        values,
    )
    row = db.execute(
        "SELECT id FROM corporate_actions WHERE symbol=? AND action_type=? AND ex_date=? AND purpose=?",
        (item["symbol"], item["action_type"], item["ex_date"], item.get("purpose", "")),
    ).fetchone()
    return int(row[0])


def adjust_candles(db: sqlite3.Connection, symbol: str, candles: list[Candle],
                   mode: str = "split_bonus") -> list[Candle]:
    """Return adjusted candles without altering source rows.

    Actions on an ex-date affect only candles strictly before that date.
    'split_bonus' handles verified share-structure actions only. Dividend,
    rights, demerger and merger events are intentionally not guessed here.
    """
    if mode == "raw":
        return candles
    if mode not in {"split_bonus", "total_return"}:
        raise ValueError("mode must be raw, split_bonus, or total_return")
    clean = symbol.strip().upper()
    if not candles:
        return candles
    first_day = datetime.fromtimestamp(candles[0].time, IST).date().isoformat()
    last_day = datetime.fromtimestamp(candles[-1].time, IST).date().isoformat()
    rows = db.execute("""
        SELECT ex_date, action_type, price_factor, volume_factor
        FROM corporate_actions
        WHERE symbol=? AND verification_status='verified'
          AND adjustment_status='applied'
          AND ex_date > ? AND ex_date <= ?
        ORDER BY ex_date
    """, (clean, first_day, last_day)).fetchall()
    actions = [dict(row) for row in rows
               if row["action_type"] in SUPPORTED_PRICE_FACTOR_TYPES
               and row["price_factor"] is not None and row["volume_factor"] is not None]
    if not actions:
        return candles
    result = []
    for candle in candles:
        candle_day = datetime.fromtimestamp(candle.time, IST).date().isoformat()
        price_factor = 1.0
        volume_factor = 1.0
        for action in actions:
            if candle_day < action["ex_date"]:
                price_factor *= float(action["price_factor"])
                volume_factor *= float(action["volume_factor"])
        result.append(Candle(
            time=candle.time,
            open=candle.open * price_factor,
            high=candle.high * price_factor,
            low=candle.low * price_factor,
            close=candle.close * price_factor,
            volume=round(candle.volume * volume_factor),
        ))
    return result


def action_status(db: sqlite3.Connection) -> dict[str, Any]:
    counts = {row["verification_status"]: row["count"] for row in db.execute(
        "SELECT verification_status, COUNT(*) AS count FROM corporate_actions GROUP BY verification_status"
    )}
    upcoming = [dict(row) for row in db.execute("""
        SELECT symbol, action_type, ex_date, purpose, ratio_text, verification_status,
               adjustment_status, source_url
        FROM corporate_actions WHERE ex_date >= date('now')
        ORDER BY ex_date LIMIT 100
    """)]
    return {
        "total_events": sum(counts.values()),
        "verified_events": counts.get("verified", 0),
        "pending_events": counts.get("pending", 0),
        "rejected_events": counts.get("rejected", 0),
        "automatic_adjustment_types": sorted(SUPPORTED_PRICE_FACTOR_TYPES),
        "dividend_total_return": "not_yet_implemented",
        "historical_import": "not_yet_implemented",
        "automatic_nse_refresh": "not_yet_implemented",
        "upcoming_events": upcoming,
    }
