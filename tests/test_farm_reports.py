from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from PIL import Image

from e4kbot.farm_reports import FarmLedger, FarmReport, image_identity, unread_battle_rows


class FarmLedgerTests(unittest.TestCase):
    def test_image_identity_is_stable_and_content_sensitive(self) -> None:
        first = Image.new("RGB", (8, 8), "white")
        same = Image.new("RGB", (8, 8), "white")
        other = Image.new("RGB", (8, 8), "black")
        self.assertEqual(image_identity(first), image_identity(same))
        self.assertNotEqual(image_identity(first), image_identity(other))

    def test_append_deduplicates_and_aggregates_only_known_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ledger = FarmLedger(Path(tmp) / "ledger.jsonl")
            report = FarmReport(
                id="report-1",
                captured_at=1.0,
                world="Великая империя",
                own_soldiers=20,
                own_losses=0,
                defender_soldiers=3,
                defender_losses=-3,
                resources={"wood": 66, "food": 173},
                rewards={"xp": 4},
            )
            self.assertTrue(ledger.append(report))
            self.assertFalse(ledger.append(report))
            summary = ledger.summary()
            self.assertEqual(summary["reports"], 1)
            self.assertEqual(summary["resources"]["wood"], 66)
            self.assertEqual(summary["own_losses"], 0)
            self.assertEqual(summary["by_world"]["Великая империя"]["reports"], 1)

    def test_unread_battle_rows_match_victory_template(self) -> None:
        from e4kbot.paths import ROOT

        crop = ROOT / "assets" / "messages" / "attack_victory_row.png"
        if not crop.exists():
            self.skipTest("inbox victory crop not on disk yet")
        row = Image.open(crop).convert("RGB")
        canvas = Image.new("RGB", (max(900, row.width + 80), max(1600, row.height + 500)), (214, 196, 150))
        canvas.paste(row, (40, 420))
        rows = unread_battle_rows(canvas)
        self.assertGreaterEqual(len(rows), 1)
        self.assertTrue(any("побед" in str(item[2]).lower() for item in rows))


if __name__ == "__main__":
    unittest.main()
