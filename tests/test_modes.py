from __future__ import annotations

import unittest

from e4kbot.config import DEFAULTS, deep_merge
from e4kbot.modes.base import StubMode
from e4kbot.modes.catalog import MODE_BY_ID, MODES, catalog_payload
from e4kbot.runtime.scheduler import pick_next_step, snapshot, steps
from e4kbot.state import StateStore


class ModeCatalogTests(unittest.TestCase):
    def test_official_names_and_stub_coverage(self) -> None:
        ids = {mode.id for mode in MODES}
        self.assertIn("robber_barons", ids)
        self.assertEqual(MODE_BY_ID["robber_barons"].status, "live")
        self.assertEqual(MODE_BY_ID["storm_forts"].official_name, "Storm Forts")
        self.assertEqual(MODE_BY_ID["barbarian_towers"].status, "live")
        self.assertEqual(MODE_BY_ID["desert_towers"].status, "live")
        self.assertEqual(MODE_BY_ID["cultist_towers"].status, "live")
        self.assertEqual(MODE_BY_ID["storm_forts"].status, "live")
        self.assertEqual(MODE_BY_ID["barbarian_towers"].kingdom_ru, "Вечнохолодный ледник")
        self.assertEqual(MODE_BY_ID["desert_fortresses"].official_name, "Desert Fortresses")
        self.assertEqual(MODE_BY_ID["cultist_towers"].official_name, "Cultist Towers")
        self.assertEqual(MODE_BY_ID["dragons"].official_name, "Dragons")
        self.assertEqual(MODE_BY_ID["nomad_camps"].official_name, "Nomad Invasion / Nomad Camps")
        self.assertEqual(MODE_BY_ID["nomad_camps"].status, "live")
        self.assertEqual(MODE_BY_ID["samurai_camps"].official_name, "Samurai Invasion / Samurai Camps")
        self.assertEqual(MODE_BY_ID["samurai_camps"].status, "live")
        self.assertEqual(MODE_BY_ID["bloodcrows"].official_name, "Bloodcrow Invasion")
        self.assertEqual(MODE_BY_ID["alien_castles"].official_name, "Alien Invasion / Alien Castles")
        self.assertEqual(MODE_BY_ID["alien_castles"].title_ru, "Вторжение чужеземцев")
        self.assertEqual(MODE_BY_ID["nomad_camps"].title_ru, "Вторжение кочевников")
        self.assertEqual(MODE_BY_ID["samurai_camps"].title_ru, "Нашествие самураев")
        stubs = [mode.id for mode in MODES if mode.status == "stub"]
        self.assertGreaterEqual(len(stubs), 5)
        self.assertEqual(len(catalog_payload()), len(MODES))

    def test_stub_mode_does_not_claim_a_send(self) -> None:
        self.assertEqual(StubMode("dragons").run_cycle(), "stub:dragons")


class CampaignSchedulerTests(unittest.TestCase):
    def test_fills_live_quota_then_skips_stub_without_waiting(self) -> None:
        store = StateStore(path=self._tmp("state.json"))
        config = deep_merge(
            DEFAULTS,
            {
                "campaign": {
                    "enabled": True,
                    "fill_without_waiting_returns": True,
                    "queue": [
                        {"mode": "robber_barons", "count": 20, "enabled": True},
                        {"mode": "dragons", "count": 5, "enabled": True},
                        {"mode": "storm_forts", "count": 10, "enabled": True},
                    ],
                }
            },
        )
        first = pick_next_step(config, store)
        self.assertIsNotNone(first)
        self.assertEqual(first.mode_id, "robber_barons")
        store.skip_mode("robber_barons")
        second = pick_next_step(config, store)
        self.assertEqual(second.mode_id, "storm_forts")
        store.skip_mode("storm_forts")
        looped = pick_next_step(config, store)
        self.assertEqual(looped.mode_id, "robber_barons")
        snap = snapshot(config, store)
        self.assertTrue(snap["fill_without_waiting_returns"])
        self.assertEqual(len(steps(config, store)), 12)

    def test_only_enabled_barons_are_picked(self) -> None:
        from e4kbot.runtime.scheduler import apply_campaign_queue

        store = StateStore(path=self._tmp("state.json"))
        config = deep_merge(DEFAULTS, {})
        apply_campaign_queue(
            config,
            enabled_modes={
                "robber_barons": True,
                "nomad_camps": False,
                "samurai_camps": False,
                "alien_castles": False,
            },
        )
        step = pick_next_step(config, store)
        self.assertIsNotNone(step)
        self.assertEqual(step.mode_id, "robber_barons")
        enabled = [item.mode_id for item in steps(config, store) if item.enabled]
        self.assertEqual(enabled, ["robber_barons"])

    def test_priority_order_skips_disabled_toggles(self) -> None:
        store = StateStore(path=self._tmp("state.json"))
        all_on = deep_merge(
            DEFAULTS,
            {
                "campaign": {
                    "enabled": True,
                    "queue": [
                        {"mode": "nomad_camps", "count": 44, "enabled": True},
                        {"mode": "samurai_camps", "count": 44, "enabled": True},
                        {"mode": "storm_forts", "count": 5, "enabled": True},
                        {"mode": "robber_barons", "count": 5, "enabled": True},
                        {"mode": "barbarian_towers", "count": 5, "enabled": True},
                        {"mode": "desert_towers", "count": 5, "enabled": True},
                        {"mode": "cultist_towers", "count": 5, "enabled": True},
                        {"mode": "alien_castles", "count": 6, "enabled": True},
                    ],
                }
            },
        )
        order = []
        seen = set()
        current = pick_next_step(all_on, store)
        while current is not None:
            if current.mode_id in seen:
                break
            seen.add(current.mode_id)
            order.append(current.mode_id)
            store.skip_mode(current.mode_id)
            current = pick_next_step(all_on, store)
        self.assertEqual(
            order,
            [
                "nomad_camps",
                "samurai_camps",
                "robber_barons",
                "barbarian_towers",
                "desert_towers",
                "cultist_towers",
                "storm_forts",
            ],
        )
        barons_only = deep_merge(
            DEFAULTS,
            {
                "campaign": {
                    "enabled": True,
                    "queue": [
                        {"mode": "nomad_camps", "count": 44, "enabled": False},
                        {"mode": "samurai_camps", "count": 44, "enabled": True},
                        {"mode": "storm_forts", "count": 5, "enabled": False},
                        {"mode": "robber_barons", "count": 5, "enabled": True},
                        {"mode": "alien_castles", "count": 6, "enabled": False},
                    ],
                }
            },
        )
        store.live.skipped_modes = []
        store.live.session_by_mode = {}
        first = pick_next_step(barons_only, store)
        self.assertEqual(first.mode_id, "samurai_camps")
        store.skip_mode("samurai_camps")
        second = pick_next_step(barons_only, store)
        self.assertEqual(second.mode_id, "robber_barons")
        store.skip_mode("robber_barons")
        looped = pick_next_step(barons_only, store)
        self.assertEqual(looped.mode_id, "samurai_camps")

    def test_one_world_five_attacks_loops_until_disabled(self) -> None:
        store = StateStore(path=self._tmp("state.json"))
        config = deep_merge(
            DEFAULTS,
            {
                "campaign": {
                    "enabled": True,
                    "queue": [
                        {"mode": "barbarian_towers", "count": 5, "enabled": True},
                    ],
                }
            },
        )
        first = pick_next_step(config, store)
        self.assertEqual(first.mode_id, "barbarian_towers")
        store.live.session_by_mode = {"barbarian_towers": 5}
        again = pick_next_step(config, store)
        self.assertEqual(again.mode_id, "barbarian_towers")
        self.assertEqual(int((store.live.session_by_mode or {}).get("barbarian_towers") or 0), 0)

    def test_skip_mode_does_not_fake_successful_sends(self) -> None:
        store = StateStore(path=self._tmp("state.json"))
        config = deep_merge(
            DEFAULTS,
            {
                "campaign": {
                    "enabled": True,
                    "queue": [
                        {"mode": "robber_barons", "count": 5, "enabled": True},
                        {"mode": "barbarian_towers", "count": 5, "enabled": True},
                    ],
                }
            },
        )
        store.live.session_by_mode = {"robber_barons": 2}
        store.skip_mode("robber_barons")
        barons = next(item for item in steps(config, store) if item.mode_id == "robber_barons")
        self.assertEqual(barons.sent, 2)
        self.assertEqual(barons.remaining, 0)
        nxt = pick_next_step(config, store)
        self.assertIsNotNone(nxt)
        self.assertEqual(nxt.mode_id, "barbarian_towers")

    def test_numeric_commander_caps_never_stop(self) -> None:
        from e4kbot.safety import commander_number_ok, concurrent_ok

        config = {"max_commander_number": 30, "max_concurrent_attacks": 30}
        self.assertTrue(commander_number_ok(31, config)[0])
        self.assertTrue(commander_number_ok(99, config)[0])
        self.assertTrue(concurrent_ok(30, config)[0])
        self.assertTrue(concurrent_ok(40, config)[0])

    def _tmp(self, name: str):
        from pathlib import Path
        from tempfile import TemporaryDirectory

        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        return Path(folder.name) / name


if __name__ == "__main__":
    unittest.main()
