from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from e4kbot.config import DEFAULTS, deep_merge
from e4kbot.control import CONTROL
from e4kbot.miniapp import create_app
from e4kbot.runtime.scheduler import apply_campaign_queue
from e4kbot.state import StateStore


class FastApiPanelTests(unittest.TestCase):
    def setUp(self) -> None:
        CONTROL.stop = False
        CONTROL.disable()
        self._tmp = tempfile.TemporaryDirectory()
        self.store = StateStore(Path(self._tmp.name) / "state.json")
        self.config = deep_merge(DEFAULTS, {"dry_run": True})
        apply_campaign_queue(self.config, enabled_modes={"robber_barons": True, "nomad_camps": False})
        self._save_patch = patch("e4kbot.miniapp.save_config")
        self._save_patch.start()
        self.client = TestClient(create_app(self.store, self.config))

    def tearDown(self) -> None:
        self._save_patch.stop()
        CONTROL.stop = False
        CONTROL.enable()
        self._tmp.cleanup()

    def test_health_reports_fastapi_react_stack(self) -> None:
        res = self.client.get("/health")
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["stack"], "fastapi+react")

    def test_settings_and_control_wire_barons_toggle(self) -> None:
        settings = self.client.get("/api/settings").json()
        self.assertEqual(settings["start_hotkey"], "NUM1")
        self.assertEqual(settings["pause_hotkey"], "NUM0")
        self.assertIn("robber_barons", settings["enabled_modes"])
        self.assertTrue(any(item["kingdom_ru"] == "Великая империя" for item in settings["worlds"]))
        paused = self.client.get("/api/state").json()
        self.assertTrue(paused["paused"])
        started = self.client.post("/api/control", json={"start": True}).json()
        self.assertTrue(started["enabled"])
        saved = self.client.post(
            "/api/settings",
            json={"enabled_modes": {"robber_barons": True, "nomad_camps": False, "samurai_camps": False}},
        ).json()
        self.assertEqual(saved["enabled_modes"], ["robber_barons"])
        self.assertTrue(saved["barons"])
        self.assertFalse(saved["nomads"])

    def test_farm_reports_endpoint_returns_summary_and_rows(self) -> None:
        with patch("e4kbot.miniapp.FarmLedger") as ledger:
            ledger.return_value.summary.return_value = {"reports": 2}
            ledger.return_value.rows.return_value = [{"id": "one"}, {"id": "two"}]
            body = self.client.get("/api/farm-reports").json()
        self.assertEqual(body["summary"]["reports"], 2)
        self.assertEqual(len(body["reports"]), 2)


    def test_state_exposes_live_phase_and_next_action(self) -> None:
        body = self.client.get("/api/state").json()
        self.assertIn(
            body["phase"],
            {"paused", "search", "returned_home", "deselect_home", "formation", "report_check"},
        )
        self.assertTrue(body["next_action"])
        self.assertIn("attacks_world", body)
        self.assertIn("timing_summary", body)
        self.assertIn("paused", body)
        self.assertIn("enabled", body)

    def test_player_attack_draft_persists_without_launching(self) -> None:
        payload = {
            "world": "great_empire",
            "x": 612,
            "y": 740,
            "attacks": 3,
            "commander": "auto",
            "formation": "default",
            "waves": 2,
            "flank": "center",
            "tools": "none",
            "delay_seconds": 12,
            "schedule": "",
            "stop_conditions": ["no_commanders"],
        }
        with patch("e4kbot.miniapp.DATA_DIR", Path(self._tmp.name)):
            saved = self.client.post("/api/player-attack-draft", json=payload).json()
            self.assertTrue(saved["ok"])
            self.assertFalse(saved["implemented"])
            self.assertFalse(saved["draft"]["implemented"])
            self.assertEqual(saved["draft"]["x"], 612)
            self.assertEqual(saved["draft"]["commander"], "auto")
            self.assertIn("не реализован", saved["draft"]["notice"])
            got = self.client.get("/api/player-attack-draft").json()
            self.assertFalse(got["implemented"])
            self.assertEqual(got["draft"]["attacks"], 3)
            deleted = self.client.delete("/api/player-attack-draft").json()
            self.assertTrue(deleted["ok"])
            self.assertIsNone(deleted["draft"])
            empty = self.client.get("/api/player-attack-draft").json()
            self.assertIsNone(empty["draft"])

    def test_player_attack_draft_rejects_bad_coords(self) -> None:
        with patch("e4kbot.miniapp.DATA_DIR", Path(self._tmp.name)):
            res = self.client.post(
                "/api/player-attack-draft",
                json={
                    "world": "great_empire",
                    "x": 5000,
                    "y": 1,
                    "attacks": 1,
                    "waves": 1,
                    "delay_seconds": 0,
                },
            )
        self.assertEqual(res.status_code, 422)


if __name__ == "__main__":
    unittest.main()
