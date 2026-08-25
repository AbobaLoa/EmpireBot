from __future__ import annotations

import unittest

from e4kbot.control import apply_public_settings, public_settings
from e4kbot.nomad_farm import NomadFarmSettings, NomadProgress
from e4kbot.state import NOMAD_HITS_PER_CAMP


class NomadFarmSettingsTests(unittest.TestCase):
    def test_default_levels_40_to_50_are_eleven_camps(self) -> None:
        settings = NomadFarmSettings()
        self.assertEqual(settings.levels, list(range(40, 51)))
        self.assertEqual(settings.camp_count, 11)
        self.assertEqual(settings.levels[-1], 50)

    def test_custom_range_41_to_50(self) -> None:
        settings = NomadFarmSettings.from_config(
            {"nomad_farm": {"start_level": 41, "end_level": 50, "max_attacks_per_camp": 11}}
        )
        self.assertEqual(settings.levels, list(range(41, 51)))
        self.assertEqual(settings.camp_count, 10)

    def test_default_max_attacks_matches_state_constant(self) -> None:
        settings = NomadFarmSettings()
        self.assertEqual(settings.max_attacks_per_camp, NOMAD_HITS_PER_CAMP)


class NomadProgressTests(unittest.TestCase):
    def test_advances_level_after_camp_cleared(self) -> None:
        settings = NomadFarmSettings(start_level=40, end_level=42, max_attacks_per_camp=11)
        progress = NomadProgress()
        self.assertEqual(progress.current_level(settings), 40)
        progress.advance_after_camp(settings, 40)
        self.assertEqual(progress.current_level(settings), 41)

    def test_full_cycle_marks_complete(self) -> None:
        settings = NomadFarmSettings(start_level=49, end_level=50, max_attacks_per_camp=11)
        progress = NomadProgress()
        progress.advance_after_camp(settings, 49)
        self.assertEqual(progress.current_level(settings), 50)
        progress.advance_after_camp(settings, 50)
        self.assertTrue(progress.is_complete(settings))
        self.assertIsNone(progress.current_level(settings))

    def test_reset_clears_progress(self) -> None:
        settings = NomadFarmSettings(start_level=40, end_level=50, max_attacks_per_camp=11)
        progress = NomadProgress(level_index=3, cleared_levels=[40, 41, 42])
        progress.reset()
        self.assertEqual(progress.level_index, 0)
        self.assertEqual(progress.cleared_levels, [])
        self.assertEqual(progress.current_level(settings), 40)

    def test_roundtrip_dict(self) -> None:
        original = NomadProgress(level_index=2, cleared_levels=[40, 41])
        restored = NomadProgress.from_dict(original.to_dict())
        self.assertEqual(restored.level_index, 2)
        self.assertEqual(restored.cleared_levels, [40, 41])

    def test_status_line_shows_sequence_position(self) -> None:
        settings = NomadFarmSettings(start_level=40, end_level=50, max_attacks_per_camp=11)
        progress = NomadProgress(level_index=10)
        line = progress.status_line(settings)
        self.assertIn("50", line)
        self.assertIn("11/11", line)


class NomadControlSettingsTests(unittest.TestCase):
    def test_public_settings_expose_nomad_farm(self) -> None:
        config = {
            "nomad_farm": {"start_level": 41, "end_level": 50, "max_attacks_per_camp": 11},
            "attack_delay_seconds": [8, 10],
            "cycle_pause_seconds": [0, 0],
            "baron_attacks": {},
            "modes": {},
            "control": {},
            "bluestacks": {},
        }
        settings = public_settings(config)
        self.assertEqual(settings["nomad_start_level"], 41)
        self.assertEqual(settings["nomad_end_level"], 50)
        self.assertEqual(settings["nomad_max_attacks_per_camp"], 11)

    def test_apply_updates_nomad_farm(self) -> None:
        config = {"nomad_farm": {"start_level": 40, "end_level": 50, "max_attacks_per_camp": 11}}
        apply_public_settings(
            config,
            {
                "nomad_start_level": 41,
                "nomad_end_level": 49,
                "nomad_max_attacks_per_camp": 7,
            },
        )
        self.assertEqual(config["nomad_farm"]["start_level"], 41)
        self.assertEqual(config["nomad_farm"]["end_level"], 49)
        self.assertEqual(config["nomad_farm"]["max_attacks_per_camp"], 7)


class NomadVisionTests(unittest.TestCase):
    def test_parse_camp_level(self) -> None:
        from e4kbot.vision import parse_camp_level

        self.assertEqual(parse_camp_level("41"), 41)
        self.assertEqual(parse_camp_level("Уровень 50"), 50)
        self.assertEqual(parse_camp_level("1 05/7"), 7)
        self.assertIsNone(parse_camp_level("нет уровня"))
