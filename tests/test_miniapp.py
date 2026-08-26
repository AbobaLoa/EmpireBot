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


if __name__ == "__main__":
    unittest.main()
