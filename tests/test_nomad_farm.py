from __future__ import annotations

import unittest

from e4kbot.control import apply_public_settings, public_settings
from e4kbot.nomad_farm import NomadFarmSettings, NomadProgress


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

    def test_clamps_out_of_range_values(self) -> None:
        settings = NomadFarmSettings.from_config(
            {"nomad_farm": {"start_level": 0, "end_level": 120, "max_attacks_per_camp": 200}}
        )
        self.assertEqual(settings.start_level, 1)
        self.assertEqual(settings.end_level, 99)
        self.assertEqual(settings.max_attacks_per_camp, 99)


class NomadProgressTests(unittest.TestCase):
    def test_advances_after_max_attacks_per_camp(self) -> None:
        settings = NomadFarmSettings(start_level=40, end_level=42, max_attacks_per_camp=11)
        progress = NomadProgress()
        self.assertEqual(progress.current_level(settings), 40)
        for _ in range(10):
            self.assertFalse(progress.record_success(settings, 40))
            self.assertEqual(progress.current_level(settings), 40)
        self.assertTrue(progress.record_success(settings, 40))
        self.assertEqual(progress.current_level(settings), 41)
        self.assertEqual(progress.attacks_on(40), 11)

    def test_full_cycle_marks_complete(self) -> None:
        settings = NomadFarmSettings(start_level=49, end_level=50, max_attacks_per_camp=2)
        progress = NomadProgress()
        progress.record_success(settings, 49)
        progress.record_success(settings, 49)
        self.assertEqual(progress.current_level(settings), 50)
        progress.record_success(settings, 50)
        progress.record_success(settings, 50)
        self.assertTrue(progress.is_complete(settings))
        self.assertIsNone(progress.current_level(settings))

    def test_reset_clears_progress(self) -> None:
        settings = NomadFarmSettings(start_level=40, end_level=50, max_attacks_per_camp=11)
        progress = NomadProgress(level_index=3, attacks_by_level={40: 11, 41: 5})
        progress.reset()
        self.assertEqual(progress.level_index, 0)
        self.assertEqual(progress.attacks_by_level, {})
        self.assertEqual(progress.current_level(settings), 40)

    def test_roundtrip_dict(self) -> None:
        original = NomadProgress(level_index=2, attacks_by_level={40: 11, 41: 3})
        restored = NomadProgress.from_dict(original.to_dict())
        self.assertEqual(restored.level_index, 2)
        self.assertEqual(restored.attacks_by_level, {40: 11, 41: 3})

    def test_status_line_shows_attack_number(self) -> None:
        settings = NomadFarmSettings(start_level=40, end_level=50, max_attacks_per_camp=11)
        progress = NomadProgress(attacks_by_level={40: 10})
        line = progress.status_line(settings)
        self.assertIn("40", line)
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


class NomadClientRoutingTests(unittest.TestCase):
    def test_on_screen_attack_routes_nomad_kind(self) -> None:
        from unittest.mock import Mock

        from e4kbot.client import BlueStacksEngine

        engine = BlueStacksEngine.__new__(BlueStacksEngine)
        engine.on_screen_nomad_attack = Mock(return_value="nomad")
        result = BlueStacksEngine.on_screen_attack(engine, "nomad")
        self.assertEqual(result, "nomad")
        engine.on_screen_nomad_attack.assert_called_once_with("nomad")
