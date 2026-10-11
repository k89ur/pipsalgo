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

    def test_classifier_parses_bonus_ratio_before_or_after_bonus_word(self) -> None:
        from app.corporate_actions import _classify_subjects

        for purpose in ("Bonus 2:1", "Issue of Bonus Shares in the ratio of 2:1"):
            actions = _classify_subjects(purpose)
            self.assertEqual(actions[0][0], "bonus")
            self.assertAlmostEqual(actions[0][1], 1 / 3)
            self.assertAlmostEqual(actions[0][2], 3)

    def test_classifier_parses_face_value_split(self) -> None:
        from app.corporate_actions import _classify_subjects

        actions = _classify_subjects(
            "Face Value Split (Sub-Division) - From Rs 10/- Per Share To Rs 2/- Per Share"
        )
        self.assertEqual(actions[0][0], "split")
        self.assertAlmostEqual(actions[0][1], 0.2)
        self.assertAlmostEqual(actions[0][2], 5)

    def test_classifier_preserves_combined_bonus_and_split(self) -> None:
        from app.corporate_actions import _classify_subjects

        actions = _classify_subjects(
            "Face Value Split from Rs 10/- Per Share To Rs 2/- Per Share and Bonus 2:1"
        )
        self.assertEqual({action[0] for action in actions}, {"split", "bonus"})
        factors = {action[0]: action[1] for action in actions}
        self.assertAlmostEqual(factors["split"], 0.2)
        self.assertAlmostEqual(factors["bonus"], 1 / 3)

    def test_two_same_date_actions_compound_on_pre_ex_date_candles(self) -> None:
        record_action(self.db, {
            "symbol": "TEST",
            "action_type": "split",
            "ex_date": "2026-01-05",
            "purpose": "combined split",
            "verification_status": "verified",
            "adjustment_status": "applied",
            "price_factor": 0.2,
            "volume_factor": 5,
        })
        record_action(self.db, {
            "symbol": "TEST",
            "action_type": "bonus",
            "ex_date": "2026-01-05",
            "purpose": "combined bonus",
            "verification_status": "verified",
            "adjustment_status": "applied",
            "price_factor": 1 / 3,
            "volume_factor": 3,
        })
        candles = [
            Candle(epoch("2026-01-02"), 100, 110, 90, 105, 1000),
            Candle(epoch("2026-01-05"), 10, 12, 9, 11, 15000),
        ]
        adjusted = adjust_candles(self.db, "TEST", candles, "split_bonus")
        self.assertAlmostEqual(adjusted[0].open, 100 * 0.2 / 3)
        self.assertAlmostEqual(adjusted[0].close, 105 * 0.2 / 3)
        self.assertEqual(adjusted[0].volume, 15000)
        self.assertEqual(adjusted[1], candles[1])

    def test_applies_later_effective_action_even_when_chart_window_ends_before_ex_date(self) -> None:
        record_action(self.db, {
            "symbol": "TEST",
            "action_type": "bonus",
            "ex_date": "2026-01-05",
            "purpose": "later bonus",
            "verification_status": "verified",
            "adjustment_status": "applied",
            "price_factor": 0.5,
            "volume_factor": 2,
        })
        candles = [Candle(epoch("2026-01-02"), 100, 110, 90, 105, 1000)]
        adjusted = adjust_candles(self.db, "TEST", candles, "split_bonus")
        self.assertEqual(adjusted[0].close, 52.5)
        self.assertEqual(adjusted[0].volume, 2000)


if __name__ == "__main__":
    unittest.main()
