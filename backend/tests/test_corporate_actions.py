from __future__ import annotations

import sqlite3
import unittest
from datetime import datetime, timedelta, timezone

from app.corporate_actions import (
    adjust_candles,
    initialize_corporate_actions,
    record_action,
    validate_action,
)
from app.providers.base import Candle


IST = timezone(timedelta(hours=5, minutes=30))


def epoch(day: str) -> int:
    return int(datetime.fromisoformat(day).replace(tzinfo=IST).timestamp())


class CorporateActionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        initialize_corporate_actions(self.db)

    def tearDown(self) -> None:
        self.db.close()

    def test_reverse_split_factors_are_valid(self) -> None:
        item = validate_action({
            "symbol": "TEST",
            "action_type": "consolidation",
            "ex_date": "2026-01-05",
            "verification_status": "verified",
            "price_factor": 10,
            "volume_factor": 0.1,
        })
        self.assertEqual(item["price_factor"], 10)
        self.assertEqual(item["volume_factor"], 0.1)

    def test_adjusts_only_pre_ex_date_candles_and_preserves_raw(self) -> None:
        record_action(self.db, {
            "symbol": "TEST",
            "action_type": "bonus",
            "ex_date": "2026-01-05",
            "purpose": "1:1 bonus",
            "verification_status": "verified",
            "adjustment_status": "applied",
            "price_factor": 0.5,
            "volume_factor": 2,
            "source_url": "https://www.nseindia.com/",
        })
        candles = [
            Candle(epoch("2026-01-02"), 100, 110, 90, 105, 1000),
            Candle(epoch("2026-01-05"), 52, 55, 50, 53, 2000),
        ]
        adjusted = adjust_candles(self.db, "TEST", candles, "split_bonus")
        self.assertEqual(adjusted[0].close, 52.5)
        self.assertEqual(adjusted[0].volume, 2000)
        self.assertEqual(adjusted[1], candles[1])
        self.assertEqual(candles[0].close, 105)

    def test_total_return_is_not_claimed_until_implemented(self) -> None:
        with self.assertRaises(NotImplementedError):
            adjust_candles(self.db, "TEST", [], "total_return")


if __name__ == "__main__":
    unittest.main()
