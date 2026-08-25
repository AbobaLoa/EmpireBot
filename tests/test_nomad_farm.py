from __future__ import annotations

import time
import unittest

from e4kbot.control import apply_public_settings, public_settings
from e4kbot.nomad_farm import NomadCampRecord, NomadFarmProgress, NomadFarmSettings, camp_key
from e4kbot.state import NOMAD_CAMP_COOLDOWN_SEC, NOMAD_HITS_PER_CAMP, StateStore


class NomadFarmSettingsTests(unittest.TestCase):
    def test_default_four_camps_eleven_attacks(self) -> None:
        settings = NomadFarmSettings()
        self.assertEqual(settings.start_level, 41)
        self.assertEqual(settings.max_attacks_per_camp, NOMAD_HITS_PER_CAMP)
        self.assertEqual(settings.num_camps, 4)
        self.assertEqual(settings.camp_cooldown_hours, 1.5)
        self.assertEqual(settings.end_level, 50)

    def test_from_config(self) -> None:
        settings = NomadFarmSettings.from_config(
            {
                "nomad_farm": {
                    "start_level": 41,
                    "max_attacks_per_camp": 11,
                    "camp_cooldown_hours": 1.5,
                    "num_camps": 4,
                }
            }
        )
        self.assertEqual(settings.start_level, 41)
        self.assertEqual(settings.end_level, 50)
        self.assertEqual(settings.camp_cooldown_sec, 90 * 60)

    def test_level_progression_on_one_camp(self) -> None:
        settings = NomadFarmSettings(start_level=41, max_attacks_per_camp=11)
        self.assertEqual(settings.level_after_attacks(0), 41)
        self.assertEqual(settings.level_after_attacks(1), 42)
        self.assertEqual(settings.level_after_attacks(9), 50)
        self.assertEqual(settings.level_after_attacks(10), 50)
        self.assertEqual(settings.attacks_remaining(0), 11)
        self.assertEqual(settings.attacks_remaining(10), 1)
        self.assertEqual(settings.attacks_remaining(11), 0)


class NomadFarmProgressTests(unittest.TestCase):
    def test_eleven_attacks_same_camp_then_cooldown(self) -> None:
        settings = NomadFarmSettings(start_level=41, max_attacks_per_camp=11)
        progress = NomadFarmProgress()
        coords = (100, 200)
        now = time.time()
        for index in range(11):
            record = progress.record_attack(coords, settings, now=now + index)
            self.assertEqual(record.attacks_done, index + 1)
        self.assertEqual(record.current_level, 50)
        self.assertGreater(record.cooldown_until, now)
        self.assertFalse(progress.camp_available(coords, settings, now=now + 10))
        self.assertIsNone(progress.active_camp)

    def test_rotation_between_four_camps(self) -> None:
        settings = NomadFarmSettings(num_camps=4, max_attacks_per_camp=11)
        progress = NomadFarmProgress()
        camps = [(10, 20), (30, 40), (50, 60), (70, 80)]
        first = progress.pick_next_camp(camps, settings)
        self.assertEqual(first, (10, 20))
        progress.record_attack(first, settings)
        second = progress.pick_next_camp(camps, settings)
        self.assertEqual(second, (10, 20))
        for _ in range(10):
            progress.record_attack(first, settings)
        self.assertTrue(progress.camp_finished(first, settings))
        third = progress.pick_next_camp(camps, settings)
        self.assertEqual(third, (30, 40))

    def test_cooldown_reset_allows_refarm(self) -> None:
        settings = NomadFarmSettings(start_level=41, max_attacks_per_camp=11, camp_cooldown_hours=1.5)
        progress = NomadFarmProgress()
        coords = (5, 6)
        started = 1_000_000.0
        for _ in range(11):
            progress.record_attack(coords, settings, now=started)
        self.assertFalse(progress.camp_available(coords, settings, now=started + 60))
        reset = progress.reset_camp_if_cooldown_expired(
            coords, settings, now=started + settings.camp_cooldown_sec + 1
        )
        self.assertTrue(reset)
        record = progress.get_camp(coords, settings)
        self.assertEqual(record.attacks_done, 0)
        self.assertEqual(record.current_level, 41)
        self.assertTrue(progress.camp_available(coords, settings, now=started + settings.camp_cooldown_sec + 1))

    def test_status_line_shows_camp_progress(self) -> None:
        settings = NomadFarmSettings(start_level=41, max_attacks_per_camp=11)
        progress = NomadFarmProgress()
        coords = (111, 222)
        progress.record_attack(coords, settings)
        line = progress.status_line(settings, coords)
        self.assertIn("111", line)
        self.assertIn("42", line)
        self.assertIn("2/11", line)

    def test_roundtrip_dict(self) -> None:
        progress = NomadFarmProgress(
            camps={
                camp_key((1, 2)): NomadCampRecord(coords=[1, 2], attacks_done=3, current_level=44).to_dict()
            },
            active_camp=camp_key((1, 2)),
        )
        restored = NomadFarmProgress.from_dict(progress.to_dict())
        self.assertEqual(restored.active_camp, "1:2")
        self.assertEqual(restored.camps["1:2"]["attacks_done"], 3)


class NomadControlSettingsTests(unittest.TestCase):
    def test_public_settings_expose_nomad_farm(self) -> None:
        config = {
            "nomad_farm": {
                "start_level": 41,
                "max_attacks_per_camp": 11,
                "camp_cooldown_hours": 1.5,
                "num_camps": 4,
            },
            "attack_delay_seconds": [8, 10],
            "cycle_pause_seconds": [0, 0],
            "baron_attacks": {},
            "modes": {},
            "control": {},
            "bluestacks": {},
        }
        settings = public_settings(config)
        self.assertEqual(settings["nomad_start_level"], 41)
        self.assertEqual(settings["nomad_max_attacks_per_camp"], 11)
        self.assertEqual(settings["nomad_camp_cooldown_hours"], 1.5)
        self.assertEqual(settings["nomad_num_camps"], 4)

    def test_apply_updates_nomad_farm(self) -> None:
        config = {
            "nomad_farm": {
                "start_level": 41,
                "max_attacks_per_camp": 11,
                "camp_cooldown_hours": 1.5,
                "num_camps": 4,
            }
        }
        apply_public_settings(
            config,
            {
                "nomad_start_level": 42,
                "nomad_max_attacks_per_camp": 7,
                "nomad_camp_cooldown_hours": 2.0,
                "nomad_num_camps": 3,
            },
        )
        self.assertEqual(config["nomad_farm"]["start_level"], 42)
        self.assertEqual(config["nomad_farm"]["max_attacks_per_camp"], 7)
        self.assertEqual(config["nomad_farm"]["camp_cooldown_hours"], 2.0)
        self.assertEqual(config["nomad_farm"]["num_camps"], 3)


class NomadStateCooldownTests(unittest.TestCase):
    def test_store_resets_camp_after_cooldown(self) -> None:
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(path=Path(tmp) / "state.json")
            store.nomad_cooldown_sec = float(NOMAD_CAMP_COOLDOWN_SEC)
            coords = (110, 220)
            store.set_nomad_remaining(coords, 11)
            started = time.time()
            for index in range(11):
                store.register_march(index + 1, index + 1, "nomad", 0, coords[0], coords[1], 30)
            self.assertFalse(store.camp_has_nomad_budget(coords))
            march = store.live.marches[-1]
            self.assertAlmostEqual(
                march.cooldown_until - started,
                NOMAD_CAMP_COOLDOWN_SEC,
                delta=5,
            )
            store.live.cooldowns[f"nomad:0:{coords[0]}:{coords[1]}"] = started - 1
            store.live.nomad_farm = {
                "camps": {
                    camp_key(coords): {
                        "coords": list(coords),
                        "attacks_done": 11,
                        "current_level": 50,
                        "cooldown_until": started - 1,
                    }
                }
            }
            self.assertTrue(store.camp_has_nomad_budget(coords))
            self.assertEqual(store.target_hits("nomad", coords), 0)


class NomadVisionTests(unittest.TestCase):
    def test_parse_camp_level(self) -> None:
        from e4kbot.vision import parse_camp_level

        self.assertEqual(parse_camp_level("41"), 41)
        self.assertEqual(parse_camp_level("Уровень 50"), 50)
        self.assertEqual(parse_camp_level("1 05/7"), 7)
        self.assertIsNone(parse_camp_level("нет уровня"))
