from __future__ import annotations

import unittest

from e4kbot.bluestacks import mapped_playfield, score_game_window


class WindowTargetingTests(unittest.TestCase):
    def test_empirebot_panel_is_never_the_game_window(self) -> None:
        hints = ["BlueStacks", "HD-Player", "Empire"]
        self.assertLess(score_game_window("EmpireBot", hints), 0)
        self.assertGreater(
            score_game_window("BlueStacks App Player", hints),
            score_game_window("EmpireBot", hints),
        )

    def test_prefers_bluestacks_over_generic_empire_title(self) -> None:
        hints = ["BlueStacks", "HD-Player", "Empire"]
        self.assertGreater(
            score_game_window("BlueStacks App Player", hints),
            score_game_window("Cursor", hints),
        )
        self.assertEqual(score_game_window("EmpireBot", hints, process="python.exe"), -1)
        self.assertGreater(score_game_window("HD-Player", hints, process="HD-Player.exe"), 0)

    def test_bare_empire_does_not_match_random_windows(self) -> None:
        self.assertEqual(score_game_window("Empire State of Mind", ["Empire"]), 0)

    def test_letterbox_keeps_900x1600_inside_wide_window(self) -> None:
        ox, oy, width, height = mapped_playfield(1200, 1600, 900, 1600)
        self.assertEqual((ox, oy, width, height), (150, 0, 900, 1600))

    def test_same_aspect_uses_full_client(self) -> None:
        self.assertEqual(mapped_playfield(450, 800, 900, 1600), (0, 0, 450, 800))
