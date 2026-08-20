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
        self.assertEqual(MODE_BY_ID["barbarian_towers"].kingdom_en, "Everwinter Glacier")
        self.assertEqual(MODE_BY_ID["desert_fortresses"].official_name, "Desert Fortresses")
        self.assertEqual(MODE_BY_ID["cultist_towers"].official_name, "Cultist Towers")
        self.assertEqual(MODE_BY_ID["dragons"].official_name, "Dragons")
        self.assertEqual(MODE_BY_ID["nomad_camps"].official_name, "Nomad Invasion / Nomad Camps")
        self.assertEqual(MODE_BY_ID["samurai_camps"].official_name, "Samurai Invasion / Samurai Camps")
        self.assertEqual(MODE_BY_ID["bloodcrows"].official_name, "Bloodcrow Invasion")
        self.assertEqual(MODE_BY_ID["alien_castles"].official_name, "Alien Invasion / Alien Castles")
        stubs = [mode.id for mode in MODES if mode.status == "stub"]
        self.assertGreaterEqual(len(stubs), 10)
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
        store.live.session_by_mode["robber_barons"] = 20
        second = pick_next_step(config, store)
        self.assertEqual(second.mode_id, "dragons")
        self.assertEqual(second.spec.status, "stub")
        store.skip_mode("dragons")
        third = pick_next_step(config, store)
        self.assertEqual(third.mode_id, "storm_forts")
        store.skip_mode("storm_forts")
        self.assertIsNone(pick_next_step(config, store))
        snap = snapshot(config, store)
        self.assertTrue(snap["fill_without_waiting_returns"])
        self.assertEqual(len(steps(config, store)), 3)

    def _tmp(self, name: str):
        from pathlib import Path
        from tempfile import TemporaryDirectory

        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        return Path(folder.name) / name


if __name__ == "__main__":
    unittest.main()
