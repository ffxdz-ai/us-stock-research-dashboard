from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from model_v2 import build_technical_snapshot, technical_score


class TechnicalV2Tests(unittest.TestCase):
    def test_ma_window_length(self) -> None:
        for count in (19, 20, 49, 50, 199, 200):
            closes = list(range(1, count + 1))
            snapshot = build_technical_snapshot(closes)
            self.assertEqual(snapshot.ma20 is not None, count >= 20)
            self.assertEqual(snapshot.ma50 is not None, count >= 50)
            self.assertEqual(snapshot.ma200 is not None, count >= 200)

    def test_prior_high20(self) -> None:
        highs = list(range(1, 22))
        snapshot = build_technical_snapshot(highs, highs, highs)
        self.assertEqual(snapshot.high20, 21)
        self.assertEqual(snapshot.prior_high20, 20)

    def test_prior_high252(self) -> None:
        highs = list(range(1, 254))
        snapshot = build_technical_snapshot(highs, highs, highs)
        self.assertEqual(snapshot.high252, 253)
        self.assertEqual(snapshot.prior_high252, 252)

    def test_technical_score_downtrend(self) -> None:
        closes = [300 - index * 0.8 for index in range(253)]
        snapshot = build_technical_snapshot(closes, closes, closes)
        self.assertLess(technical_score(closes[-1], snapshot), 30)

    def test_technical_score_uptrend(self) -> None:
        closes = [100 + index * 0.8 for index in range(253)]
        snapshot = build_technical_snapshot(closes, closes, closes)
        self.assertGreaterEqual(technical_score(closes[-1], snapshot), 65)


if __name__ == "__main__":
    unittest.main()
