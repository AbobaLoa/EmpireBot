from __future__ import annotations

import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from e4kbot.campaign_checkpoint import (
    RESUME_WINDOW_SEC,
    apply_on_enable,
    apply_on_start,
    mark_paused,
    should_resume,
    write_checkpoint,
)
from e4kbot.config import DEFAULTS, deep_merge
from e4kbot.runtime.scheduler import pick_next_step
from e4kbot.state import StateStore


class CampaignCheckpointTests(unittest.TestCase):
    def _store(self) -> tuple[StateStore, Path]:
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        path = Path(folder.name)
        return StateStore(path=path / "state.json"), path / "campaign_checkpoint.json"

    def test_resume_within_ten_minutes(self) -> None:
        store, ckpt = self._store()
        store.live.active_mode = "barbarian_towers"
        store.live.session_by_mode = {"robber_barons": 5, "barbarian_towers": 2}
        store.live.current_world = "Вечнохолодный ледник"
        store.live.mode = "wait_commanders"
        store.live.next_attack_at = time.time() + 30
        mark_paused(store, None, path=ckpt)
        store.live.session_by_mode = {}
        store.live.active_mode = ""
        decision = apply_on_enable(store, None, path=ckpt)
        self.assertEqual(decision, "resume")
        self.assertEqual(store.live.session_by_mode["barbarian_towers"], 2)
        self.assertEqual(store.live.active_mode, "barbarian_towers")
        self.assertTrue(should_resume({"paused_at": time.time() - 60}))

    def test_restart_after_ten_minutes_starts_great_empire(self) -> None:
        store, ckpt = self._store()
        store.live.active_mode = "cultist_towers"
        store.live.session_by_mode = {"robber_barons": 5, "cultist_towers": 3}
        store.live.skipped_modes = ["storm_forts"]
        write_checkpoint(store, None, path=ckpt, pause=True, now=time.time() - RESUME_WINDOW_SEC - 5)
        decision = apply_on_start(store, None, path=ckpt)
        self.assertEqual(decision, "restart")
        self.assertEqual(store.live.session_by_mode, {})
        self.assertEqual(store.live.skipped_modes, [])
        self.assertEqual(store.live.active_mode, "robber_barons")
        self.assertEqual(store.live.current_world, "Великая империя")

    def test_fresh_campaign_queue_uses_priority_among_enabled(self) -> None:
        store, _ckpt = self._store()
        config = deep_merge(
            DEFAULTS,
            {
                "campaign": {
                    "enabled": True,
                    "queue": [
                        {"mode": "robber_barons", "count": 5, "enabled": True},
                        {"mode": "barbarian_towers", "count": 5, "enabled": True},
                        {"mode": "desert_towers", "count": 5, "enabled": True},
                        {"mode": "cultist_towers", "count": 5, "enabled": True},
                        {"mode": "storm_forts", "count": 5, "enabled": True},
                    ],
                }
            },
        )
        first = pick_next_step(config, store)
        self.assertIsNotNone(first)
        self.assertEqual(first.mode_id, "robber_barons")
        store.live.session_by_mode["robber_barons"] = 5
        second = pick_next_step(config, store)
        self.assertEqual(second.mode_id, "barbarian_towers")
        store.live.session_by_mode["barbarian_towers"] = 5
        store.live.session_by_mode["desert_towers"] = 5
        store.live.session_by_mode["cultist_towers"] = 5
        last = pick_next_step(config, store)
        self.assertEqual(last.mode_id, "storm_forts")


if __name__ == "__main__":
    unittest.main()
