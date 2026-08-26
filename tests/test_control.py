from __future__ import annotations

import unittest

from e4kbot.control import CONTROL, BotPaused, apply_public_settings, hotkey_label, normalize_hotkey, public_settings


class ControlTests(unittest.TestCase):
    def tearDown(self) -> None:
        CONTROL.stop = False
        CONTROL.enable()

    def test_normalizes_letter_and_function_keys(self) -> None:
        self.assertEqual(normalize_hotkey("n"), "N")
        self.assertEqual(normalize_hotkey("f8"), "F8")
        self.assertEqual(normalize_hotkey("num0"), "NUM0")
        self.assertEqual(normalize_hotkey("numpad0"), "NUM0")
        self.assertEqual(normalize_hotkey("kp_0"), "NUM0")
        self.assertEqual(normalize_hotkey("???"), "NUM0")
        self.assertEqual(normalize_hotkey("num1"), "NUM1")
        self.assertEqual(normalize_hotkey("numpad1"), "NUM1")
        self.assertEqual(hotkey_label("NUM1"), "Num1")

    def test_num1_tracks_numlock_on_and_off(self) -> None:
        from e4kbot.control import _start_key_down

        class FakeUser32:
            def __init__(self, *, numpad1: int = 0, end: int = 0, numlock: int = 1) -> None:
                self.numpad1 = numpad1
                self.end = end
                self.numlock = numlock

            def GetAsyncKeyState(self, vk: int) -> int:
                if vk == 0x61:
                    return self.numpad1
                if vk == 0x23:
                    return self.end
                return 0

            def GetKeyState(self, vk: int) -> int:
                return self.numlock if vk == 0x90 else 0

        self.assertTrue(_start_key_down(FakeUser32(numpad1=0x8000, numlock=1), "NUM1"))
        self.assertFalse(_start_key_down(FakeUser32(end=0x8000, numlock=1), "NUM1"))
        self.assertTrue(_start_key_down(FakeUser32(end=0x8000, numlock=0), "NUM1"))
        self.assertFalse(_start_key_down(FakeUser32(numlock=0), "NUM1"))

    def test_start_hotkey_cannot_steal_num0(self) -> None:
        from e4kbot.control import normalize_start_hotkey

        self.assertEqual(normalize_start_hotkey("NUM0"), "NUM1")
        self.assertEqual(normalize_start_hotkey("numpad0"), "NUM1")

    def test_apply_campaign_toggles_do_not_wipe_other_modes(self) -> None:
        config = {
            "current_target_kind": "baron",
            "dry_run": False,
            "max_concurrent_attacks": 30,
            "attack_delay_seconds": [8, 10],
            "cycle_pause_seconds": [0, 0],
            "baron_attacks": {"use_feathers": True, "gold_fallback_when_no_feathers": True},
            "modes": {"barons": False, "nomads": False, "shogun": False},
            "bluestacks": {"input": "mouse"},
            "control": {"hotkey": "NUM0", "start_hotkey": "NUM1", "always_on_top": True},
            "campaign": {
                "enabled": True,
                "queue": [
                    {"mode": "robber_barons", "count": 20, "enabled": True},
                    {"mode": "nomad_camps", "count": 44, "enabled": True},
                    {"mode": "samurai_camps", "count": 44, "enabled": False},
                ],
            },
        }
        settings = apply_public_settings(
            config,
            {
                "enabled_modes": {
                    "robber_barons": True,
                    "nomad_camps": False,
                    "samurai_camps": False,
                    "alien_castles": False,
                }
            },
        )
        enabled = {item["mode"]: item["enabled"] for item in settings["campaign"]}
        self.assertTrue(enabled["robber_barons"])
        self.assertFalse(enabled["nomad_camps"])
        self.assertFalse(enabled["samurai_camps"])
        self.assertIn("alien_castles", enabled)
        self.assertTrue(settings["barons"])
        self.assertFalse(settings["nomads"])
        self.assertFalse(settings["shogun"])
        self.assertEqual(settings["start_hotkey"], "NUM1")
        self.assertEqual(settings["pause_hotkey"], "NUM0")
        self.assertEqual(config["current_target_kind"], "baron")

    def test_num0_tracks_numlock_on_and_off(self) -> None:
        from e4kbot.control import _pause_key_down

        class FakeUser32:
            def __init__(self, *, numpad0: int = 0, insert: int = 0, numlock: int = 1) -> None:
                self.numpad0 = numpad0
                self.insert = insert
                self.numlock = numlock

            def GetAsyncKeyState(self, vk: int) -> int:
                if vk == 0x60:
                    return self.numpad0
                if vk == 0x2D:
                    return self.insert
                return 0

            def GetKeyState(self, vk: int) -> int:
                return self.numlock if vk == 0x90 else 0

        self.assertTrue(_pause_key_down(FakeUser32(numpad0=0x8000, numlock=1), "NUM0"))
        self.assertFalse(_pause_key_down(FakeUser32(insert=0x8000, numlock=1), "NUM0"))
        self.assertTrue(_pause_key_down(FakeUser32(insert=0x8000, numlock=0), "NUM0"))
        self.assertFalse(_pause_key_down(FakeUser32(numlock=0), "NUM0"))

    def test_toggle_pauses_and_blocks_clicks(self) -> None:
        CONTROL.enable()
        CONTROL.check()
        CONTROL.disable()
        with self.assertRaises(BotPaused):
            CONTROL.check()
        CONTROL.toggle()
        CONTROL.check()
        self.assertTrue(CONTROL.is_enabled())

    def test_apply_settings_updates_target_and_hotkey(self) -> None:
        config = {
            "current_target_kind": "baron",
            "dry_run": False,
            "max_concurrent_attacks": 30,
            "attack_delay_seconds": [4, 10],
            "cycle_pause_seconds": [12, 25],
            "baron_attacks": {"use_feathers": True, "gold_fallback_when_no_feathers": True},
            "modes": {"barons": True, "nomads": True, "shogun": True},
            "bluestacks": {"input": "mouse"},
            "control": {"hotkey": "N", "always_on_top": True},
        }
        settings = apply_public_settings(
            config,
            {
                "current_target_kind": "nomad",
                "max_concurrent_attacks": 12,
                "attack_delay_min": 5,
                "attack_delay_max": 8,
                "input": "adb",
            },
        )
        self.assertEqual(config["current_target_kind"], "nomad")
        self.assertEqual(config["max_concurrent_attacks"], 12)
        self.assertEqual(config["attack_delay_seconds"], [5, 8])
        self.assertEqual(config["bluestacks"]["input"], "adb")
        self.assertEqual(settings["current_target_kind"], "nomad")
        self.assertEqual(public_settings(config)["hotkey"], "N")

    def test_start_paused_only_applies_at_startup(self) -> None:
        CONTROL.enable()
        CONTROL.configure(
            {"control": {"hotkey": "N", "start_paused": True}},
            startup=False,
        )
        self.assertTrue(CONTROL.is_enabled())
        CONTROL.configure(
            {"control": {"hotkey": "N", "start_paused": True}},
            startup=True,
        )
        self.assertFalse(CONTROL.is_enabled())
        CONTROL.enable()
        CONTROL.configure(
            {"control": {"hotkey": "N", "start_paused": True}},
            startup=False,
        )
        self.assertTrue(CONTROL.is_enabled())

    def test_enable_unblocks_wait_until_enabled(self) -> None:
        import threading
        import time

        CONTROL.disable()
        released = threading.Event()

        def waiter() -> None:
            CONTROL.wait_until_enabled()
            released.set()

        thread = threading.Thread(target=waiter, daemon=True)
        thread.start()
        time.sleep(0.05)
        self.assertFalse(released.is_set())
        CONTROL.enable()
        self.assertTrue(released.wait(timeout=1.0))

    def test_user_disable_sends_session_summary(self) -> None:
        from unittest.mock import Mock, patch

        from e4kbot.engine import AttackBot
        from e4kbot.state import LiveState

        bot = AttackBot.__new__(AttackBot)
        bot.stop = False
        bot._armed_for_report = False
        bot.client = None
        bot.store = Mock()
        bot.store.live = LiveState()
        bot.store.session_summary.return_value = {"attacks": 2, "gold": 500, "rubies": 3}
        bot.telegram = Mock()
        with patch("e4kbot.engine.apply_on_enable"), patch("e4kbot.engine.mark_paused"):
            CONTROL.enable()
            AttackBot._on_control_change(bot)
            self.assertTrue(bot._armed_for_report)
            CONTROL.disable()
            AttackBot._on_control_change(bot)
        bot.telegram.report_shutdown_summary.assert_called_once_with(
            2,
            500,
            3,
            reason="Остановлен кнопкой / горячей клавишей",
        )
        self.assertFalse(bot._armed_for_report)

    def test_five_bluestacks_misses_stop_and_summarize(self) -> None:
        from unittest.mock import Mock

        from e4kbot.engine import AttackBot, BLUESTACKS_MISS_LIMIT
        from e4kbot.state import LiveState

        bot = AttackBot.__new__(AttackBot)
        bot.config = {}
        bot.stop = False
        bot._bluestacks_misses = 0
        bot.store = Mock()
        bot.store.live = LiveState()
        bot.store.live.session_attacks = 3
        bot.store.live.session_gold = 1200
        bot.store.live.session_rubies = 8
        bot.store.session_summary.return_value = {"attacks": 3, "gold": 1200, "rubies": 8}
        bot.telegram = Mock()
        CONTROL.enable()
        for _ in range(BLUESTACKS_MISS_LIMIT - 1):
            self.assertFalse(bot.record_bluestacks_miss("no_window"))
            self.assertFalse(bot.stop)
        self.assertTrue(bot.record_bluestacks_miss("no_window"))
        self.assertTrue(bot.stop)
        self.assertFalse(CONTROL.is_enabled())
        bot.telegram.report_shutdown_summary.assert_called_once_with(
            3, 1200, 8, reason="BlueStacks не найден 5 раз подряд (no_window)"
        )

    def test_no_commanders_summary_waits_for_inflight_then_stays_enabled(self) -> None:
        import tempfile
        import time
        from pathlib import Path
        from unittest.mock import Mock

        from e4kbot.engine import AttackBot
        from e4kbot.state import StateStore

        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / "state.json")
            march = store.register_march(1, 1, "baron", 0, 100, 200, 90)
            bot = AttackBot.__new__(AttackBot)
            bot.store = store
            bot.telegram = Mock()
            bot._armed_for_report = True
            CONTROL.enable()
            bot.handle_no_commanders_result()
            self.assertTrue(CONTROL.is_enabled())
            self.assertGreater(store.live.next_attack_at, time.time())
            self.assertAlmostEqual(store.live.next_attack_at, march.return_at, delta=1)
            bot.telegram.report_shutdown_summary.assert_called_once()
            args, kwargs = bot.telegram.report_shutdown_summary.call_args
            self.assertEqual(args[:3], (1, 0, 0))
            self.assertIn("военачальник", kwargs["reason"].lower())

    def test_no_commanders_without_inflight_waits_fallback(self) -> None:
        import tempfile
        import time
        from pathlib import Path
        from unittest.mock import Mock

        from e4kbot.engine import AttackBot
        from e4kbot.state import StateStore

        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / "state.json")
            bot = AttackBot.__new__(AttackBot)
            bot.store = store
            bot.telegram = Mock()
            bot._armed_for_report = True
            CONTROL.enable()
            bot.handle_no_commanders_result()
            self.assertTrue(CONTROL.is_enabled())
            self.assertGreater(store.live.next_attack_at, time.time() + 10 * 60)
            bot.telegram.report_shutdown_summary.assert_called_once()
