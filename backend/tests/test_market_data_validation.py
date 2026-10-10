from __future__ import annotations

import unittest
from datetime import date

from app.sqlite_market_data import _parse_index_csv


class MarketDataValidationTests(unittest.TestCase):
    def test_index_parser_keeps_only_supported_indices(self):
        payload = b"""Index Name,Open Index Value,High Index Value,Low Index Value,Closing Index Value,Volume,Index Date
NIFTY 50,25000,25100,24900,25050,0,09-10-2026
NIFTY 50 USD,8485.25,8774.55,8348.00,8347.84,0,15-09-2026
"""
        rows = _parse_index_csv(payload, date(2026, 10, 9))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], "NIFTY")
        self.assertEqual(rows[0][4], 24900)
        self.assertEqual(rows[0][5], 25050)

    def test_index_parser_rejects_impossible_ohlc_relationships(self):
        payload = b"""Index Name,Open Index Value,High Index Value,Low Index Value,Closing Index Value,Volume,Index Date
NIFTY 50,25000,25100,24900,25050,0,09-10-2026
NIFTY BANK,25000,25100,24950,24940,0,09-10-2026
NIFTY IT,25000,24900,24800,24850,0,09-10-2026
"""
        rows = _parse_index_csv(payload, date(2026, 10, 9))
        self.assertEqual([row[0] for row in rows], ["NIFTY"])


if __name__ == "__main__":
    unittest.main()
