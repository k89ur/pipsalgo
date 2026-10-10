from __future__ import annotations

"""Verified corporate-action ledger and deterministic EOD adjustment engine.

Raw NSE daily_bars are never modified. Only verified split/bonus/consolidation
events with an explicit positive price factor are applied automatically.
Other event types remain recorded but unapplied until their event-specific
adjustment methodology has been verified.
"""
import math
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
        price_factor = float(price_factor)
        volume_factor = float(volume_factor)
        # Reverse splits/consolidations increase per-share prices and reduce
        # volume, so factors may be above or below 1. Only reciprocity matters.
        if (
            not math.isfinite(price_factor)
            or not math.isfinite(volume_factor)
            or price_factor <= 0
            or volume_factor <= 0
        ):
            raise ValueError("Price and volume factors must be finite positive numbers")
        if abs(price_factor * volume_factor - 1.0) > 0.03:
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
    if mode == "total_return":
        raise NotImplementedError(
            "total_return adjustment is unavailable until verified dividend "
            "cash-flow handling is implemented; use raw or split_bonus"
        )
    if mode != "split_bonus":
        raise ValueError("mode must be raw or split_bonus")
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


# NSE's official corporate-action feed. The date window is deliberately chunked
# to avoid asking the endpoint for an unbounded response.
NSE_HOME = "https://www.nseindia.com/"
NSE_ACTIONS_URL = "https://www.nseindia.com/api/corporates-corporateActions"
NSE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                  "Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": NSE_HOME,
}


def _parse_nse_date(value: Any) -> str | None:
    raw = str(value or "").strip()
    if not raw or raw in {"-", "NA", "null"}:
        return None
    for fmt in ("%d-%b-%Y", "%d-%B-%Y", "%d-%m-%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _classify_subject(subject: str) -> tuple[str, float | None, float | None, float | None]:
    """Classify action text and derive only unambiguous share-structure factors."""
    import re

    text = " ".join((subject or "").strip().lower().split())
    if "bonus" in text:
        match = re.search(r"bonus\s*(?:issue\s*)?(\d+)\s*[:/ ]\s*(\d+)", text)
        if match:
            new_shares, existing_shares = int(match.group(1)), int(match.group(2))
            if new_shares > 0 and existing_shares > 0:
                multiplier = (new_shares + existing_shares) / existing_shares
                return "bonus", 1.0 / multiplier, multiplier, None
        return "bonus", None, None, None

    if "split" in text or "sub-division" in text or "subdivision" in text:
        # NSE purpose commonly states old face value -> new face value.
        match = re.search(
            r"(?:from\s*)?(?:rs\.?\s*)?(\d+(?:\.\d+)?)\s*"
            r"(?:/-\s*)?(?:per\s*share\s*)?(?:to|into|subdivided\s*to)\s*"
            r"(?:rs\.?\s*)?(\d+(?:\.\d+)?)",
            text,
        )
        if match:
            old_face, new_face = float(match.group(1)), float(match.group(2))
            if old_face > 0 and new_face > 0:
                multiplier = old_face / new_face
                return ("split" if multiplier >= 1 else "consolidation",
                        1.0 / multiplier, multiplier, None)
        return "split", None, None, None

    if "consolidat" in text:
        return "consolidation", None, None, None
    if "dividend" in text:
        match = re.search(r"(?:rs\.?|re\.?|₹)\s*(\d+(?:\.\d+)?)", text)
        return "dividend", None, None, float(match.group(1)) if match else None
    if "right" in text:
        return "rights", None, None, None
    if "demerger" in text:
        return "demerger", None, None, None
    if "merger" in text or "amalgamation" in text:
        return "merger", None, None, None
    return "other", None, None, None


def _nse_session():
    import requests
    session = requests.Session()
    session.headers.update(NSE_HEADERS)
    # Warm cookies from the official site before calling its JSON endpoint.
    response = session.get(NSE_HOME, timeout=20)
    response.raise_for_status()
    return session


def refresh_nse_corporate_actions(
    db: sqlite3.Connection,
    *,
    start_date: date,
    end_date: date,
    session=None,
) -> dict[str, Any]:
    """Import official NSE equity actions in bounded windows.

    Events with clearly parsed bonus/split ratios are marked verified only
    after coming from the official NSE feed. Other event terms remain pending
    for event-specific review and cannot alter adjusted candles.
    """
    import json
    import requests
    from datetime import timedelta

    own_session = session is None
    session = session or _nse_session()
    result: dict[str, Any] = {
        "source": NSE_ACTIONS_URL,
        "from_date": start_date.isoformat(),
        "to_date": end_date.isoformat(),
        "windows_checked": 0,
        "rows_received": 0,
        "events_written": 0,
        "errors": [],
    }
    cursor = start_date
    try:
        while cursor <= end_date:
            chunk_end = min(cursor + timedelta(days=89), end_date)
            params = {
                "index": "equities",
                "from_date": cursor.strftime("%d-%m-%Y"),
                "to_date": chunk_end.strftime("%d-%m-%Y"),
            }
            try:
                response = session.get(NSE_ACTIONS_URL, params=params, timeout=30)
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, list):
                    raise ValueError("NSE corporate-action feed did not return a JSON list")
                result["windows_checked"] += 1
                result["rows_received"] += len(payload)
                for row in payload:
                    if not isinstance(row, dict):
                        continue
                    symbol = str(row.get("symbol") or "").strip().upper()
                    series = str(row.get("series") or "EQ").strip().upper()
                    if not symbol or series not in {"EQ", "BE"}:
                        continue
                    subject = str(row.get("subject") or row.get("purpose") or "").strip()
                    ex_date = _parse_nse_date(row.get("exDate") or row.get("ex_date"))
                    if not ex_date:
                        continue
                    action_type, price_factor, volume_factor, dividend_amount = _classify_subject(subject)
                    is_factorized = action_type in SUPPORTED_PRICE_FACTOR_TYPES and price_factor is not None
                    is_past = date.fromisoformat(ex_date) < date.today()
                    status = "verified" if is_factorized else "pending"
                    adjustment_status = "applied" if is_factorized and is_past else "not_applied"
                    record_action(db, {
                        "symbol": symbol,
                        "action_type": action_type,
                        "announcement_date": _parse_nse_date(row.get("broadcastDate") or row.get("announcementDate")),
                        "ex_date": ex_date,
                        "record_date": _parse_nse_date(row.get("recDate") or row.get("recordDate")),
                        "purpose": subject,
                        "ratio_text": subject if action_type in {"bonus", "split", "consolidation"} else "",
                        "price_factor": price_factor,
                        "volume_factor": volume_factor,
                        "dividend_amount": dividend_amount,
                        "source_url": NSE_ACTIONS_URL,
                        "source_name": "NSE official corporate actions API",
                        "verification_status": status,
                        "adjustment_status": adjustment_status,
                        "notes": "" if is_factorized else "Official NSE event imported; terms/factor require event-specific review.",
                        "source_payload": json.dumps(row, ensure_ascii=False, sort_keys=True),
                    })
                    result["events_written"] += 1
                db.commit()
            except (requests.RequestException, ValueError, TypeError) as exc:
                result["errors"].append(
                    f"{cursor.isoformat()}..{chunk_end.isoformat()}: {type(exc).__name__}: {exc}"
                )
            cursor = chunk_end + timedelta(days=1)
    finally:
        if own_session:
            session.close()
    result["status"] = "ok" if not result["errors"] else ("partial" if result["events_written"] else "error")
    return result
