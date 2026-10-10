from __future__ import annotations

import sqlite3
import unittest
from datetime import date

from app.corporate_actions import (
    _classify_subject,
    initialize_corporate_actions,
    refresh_nse_corporate_actions,
)


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, payload):
        self.payload = payload
        self.urls = []

    def get(self, url, params=None, timeout=None):
        self.urls.append((url, params))
        return FakeResponse(self.payload)

    def close(self):
        return None


class NSECorporateActionImportTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        initialize_corporate_actions(self.db)

    def tearDown(self):
        self.db.close()

    def test_bonus_factor_is_share_count_reciprocal(self):
        kind, price_factor, volume_factor, dividend = _classify_subject("BONUS 1:1")
        self.assertEqual(kind, "bonus")
        self.assertAlmostEqual(price_factor, 0.5)
        self.assertAlmostEqual(volume_factor, 2.0)
        self.assertIsNone(dividend)

    def test_split_factor_uses_face_value_change(self):
        kind, price_factor, volume_factor, _ = _classify_subject("Stock Split from Rs. 10/- per share to Rs. 2/- per share")
        self.assertEqual(kind, "split")
        self.assertAlmostEqual(price_factor, 0.2)
        self.assertAlmostEqual(volume_factor, 5.0)

    def test_unknown_terms_remain_pending(self):
        kind, price_factor, volume_factor, _ = _classify_subject("Demerger")
        self.assertEqual(kind, "demerger")
        self.assertIsNone(price_factor)
        self.assertIsNone(volume_factor)

    def test_imports_official_feed_rows_as_verified_or_pending(self):
        session = FakeSession([
            {
                "symbol": "TEST",
                "series": "EQ",
                "subject": "BONUS 1:1",
                "exDate": "01-Jan-2026",
                "recDate": "02-Jan-2026",
            },
            {
                "symbol": "OTHER",
                "series": "EQ",
                "subject": "Demerger",
                "exDate": "01-Jan-2026",
                "recDate": "-",
            },
        ])
        result = refresh_nse_corporate_actions(
            self.db,
            start_date=date(2026, 1, 1),
            end_date=date(2026, 1, 2),
            session=session,
        )
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["events_written"], 2)
        rows = list(self.db.execute(
            "SELECT symbol, verification_status, price_factor, adjustment_status "
            "FROM corporate_actions ORDER BY symbol"
        ))
        self.assertEqual(rows[0]["symbol"], "OTHER")
        self.assertEqual(rows[0]["verification_status"], "pending")
        self.assertEqual(rows[1]["symbol"], "TEST")
        self.assertEqual(rows[1]["verification_status"], "verified")
        self.assertAlmostEqual(rows[1]["price_factor"], 0.5)
        self.assertEqual(rows[1]["adjustment_status"], "applied")


if __name__ == "__main__":
    unittest.main()
