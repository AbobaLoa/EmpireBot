from __future__ import annotations

import ctypes
import ctypes.wintypes
import threading
import time
from typing import Any, Callable

from loguru import logger


class BotPaused(Exception):
    """Raised when a click/wait must abort because the operator paused the bot."""


def normalize_hotkey(raw: str | None) -> str:
    text = str(raw or "N").strip().upper()
    if text.startswith("F") and text[1:].isdigit():
        number = int(text[1:])
        if 1 <= number <= 12:
            return f"F{number}"
    if len(text) == 1 and ("A" <= text <= "Z" or "0" <= text <= "9"):
        return text
    return "N"


def _vk_code(hotkey: str) -> int:
    key = normalize_hotkey(hotkey)
    if key.startswith("F") and key[1:].isdigit():
        return 0x70 + int(key[1:]) - 1
    return ord(key)


class ControlBus:
    def __init__(self) -> None:
        self._enabled = threading.Event()
        self._enabled.set()
        self._lock = threading.Lock()
        self.stop = False
        self.hotkey = "N"
        self.always_on_top = True
        self._listeners: list[Callable[[], None]] = []
        self._hotkey_thread: threading.Thread | None = None
        self._hotkey_stop = threading.Event()
        self._hotkey_ready = threading.Event()
        self._hotkey_error = ""

    def configure(self, config: dict[str, Any], *, startup: bool = False) -> None:
        """Apply hotkey/topmost. start_paused only pauses at process start, never later."""
        control = config.get("control") or {}
        self.hotkey = normalize_hotkey(str(control.get("hotkey") or "N"))
        self.always_on_top = bool(control.get("always_on_top", True))
        if not startup:
            return
        if control.get("start_paused"):
            self.disable()
        else:
            self.enable()

    def is_enabled(self) -> bool:
        return self._enabled.is_set() and not self.stop

    def snapshot(self) -> dict[str, Any]:
        return {
            "enabled": self.is_enabled(),
            "paused": not self.is_enabled(),
            "hotkey": self.hotkey,
            "always_on_top": self.always_on_top,
            "stop": self.stop,
        }

    def on_change(self, callback: Callable[[], None]) -> None:
        self._listeners.append(callback)

    def _notify(self) -> None:
        for callback in list(self._listeners):
            try:
                callback()
            except Exception:
                logger.exception("Ошибка обработчика панели управления")

    def enable(self) -> None:
        if self.stop:
            return
        changed = not self._enabled.is_set()
        self._enabled.set()
        if changed:
            logger.info("Бот ВКЛ — клики разрешены")
            self._notify()

    def disable(self) -> None:
        changed = self._enabled.is_set()
        self._enabled.clear()
        if changed:
            logger.info("Бот ВЫКЛ — мышь свободна (клавиша {})", self.hotkey)
            self._notify()

    def toggle(self) -> None:
        if self.is_enabled():
            self.disable()
        else:
            self.enable()

    def shutdown(self) -> None:
        self.stop = True
        self._enabled.set()
        self.stop_hotkey()
        self._notify()

    def wait_until_enabled(self) -> None:
        while not self.stop:
            if self._enabled.wait(timeout=0.15):
                if self.stop:
                    return
                if self._enabled.is_set():
                    return

    def check(self) -> None:
        if self.stop:
            raise BotPaused()
        if not self._enabled.is_set():
            raise BotPaused()

    def sleep(self, seconds: float) -> None:
        deadline = time.time() + max(0.0, float(seconds))
        while time.time() < deadline:
            self.check()
            time.sleep(min(0.05, max(0.0, deadline - time.time())))

    def set_hotkey(self, hotkey: str) -> str:
        self.hotkey = normalize_hotkey(hotkey)
        self.restart_hotkey()
        logger.info("Горячая клавиша паузы: {}", self.hotkey)
        self._notify()
        return self.hotkey

    def restart_hotkey(self) -> None:
        self.stop_hotkey()
        self._hotkey_stop.clear()
        self._hotkey_ready.clear()
        self._hotkey_error = ""
        self._hotkey_thread = threading.Thread(
            target=self._hotkey_loop,
            name="e4k-hotkey",
            daemon=True,
        )
        self._hotkey_thread.start()
        self._hotkey_ready.wait(timeout=1.5)
        if self._hotkey_error:
            logger.warning("Не удалось повесить хоткей {}: {}", self.hotkey, self._hotkey_error)

    def stop_hotkey(self) -> None:
        self._hotkey_stop.set()
        thread = self._hotkey_thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=1.0)
        self._hotkey_thread = None

    def _hotkey_loop(self) -> None:
        user32 = ctypes.windll.user32
        vk = _vk_code(self.hotkey)
        self._hotkey_ready.set()
        logger.info("Глобальный хоткей {} — вкл/выкл бота", self.hotkey)
        was_down = bool(user32.GetAsyncKeyState(vk) & 0x8000)
        while not self._hotkey_stop.is_set() and not self.stop:
            is_down = bool(user32.GetAsyncKeyState(vk) & 0x8000)
            if is_down and not was_down:
                self.toggle()
            was_down = is_down
            time.sleep(0.02)


CONTROL = ControlBus()


def public_settings(config: dict[str, Any]) -> dict[str, Any]:
    delays = config.get("attack_delay_seconds") or [8, 10]
    cycles = config.get("cycle_pause_seconds") or [0, 0]
    baron = config.get("baron_attacks") or {}
    modes = config.get("modes") or {}
    control = config.get("control") or {}
    return {
        "current_target_kind": str(config.get("current_target_kind") or "baron"),
        "dry_run": bool(config.get("dry_run")),
        "max_concurrent_attacks": int(config.get("max_concurrent_attacks") or 30),
        "max_commander_number": int(config.get("max_commander_number") or 30),
        "attack_delay_min": int(delays[0]),
        "attack_delay_max": int(delays[1]),
        "cycle_pause_min": int(cycles[0]),
        "cycle_pause_max": int(cycles[1]),
        "use_feathers": bool(baron.get("use_feathers", True)),
        "gold_fallback_when_no_feathers": bool(baron.get("gold_fallback_when_no_feathers", True)),
        "barons": bool(modes.get("barons", True)),
        "nomads": bool(modes.get("nomads", True)),
        "shogun": bool(modes.get("shogun", True)),
        "hotkey": normalize_hotkey(str(control.get("hotkey") or CONTROL.hotkey)),
        "always_on_top": bool(control.get("always_on_top", True)),
        "start_paused": bool(control.get("start_paused", False)),
        "input": str((config.get("bluestacks") or {}).get("input") or "mouse"),
    }


def apply_public_settings(config: dict[str, Any], updates: dict[str, Any]) -> dict[str, Any]:
    kind = str(updates.get("current_target_kind") or config.get("current_target_kind") or "baron")
    if kind not in {"baron", "nomad", "shogun"}:
        kind = "baron"
    config["current_target_kind"] = kind
    if "dry_run" in updates:
        config["dry_run"] = bool(updates["dry_run"])
    if "max_concurrent_attacks" in updates:
        config["max_concurrent_attacks"] = max(1, min(30, int(updates["max_concurrent_attacks"])))
    if "max_commander_number" in updates:
        config["max_commander_number"] = max(1, min(30, int(updates["max_commander_number"])))
    delay_min = int(updates.get("attack_delay_min", (config.get("attack_delay_seconds") or [8, 10])[0]))
    delay_max = int(updates.get("attack_delay_max", (config.get("attack_delay_seconds") or [8, 10])[1]))
    config["attack_delay_seconds"] = [max(1, min(delay_min, delay_max)), max(1, max(delay_min, delay_max))]
    pause_min = int(updates.get("cycle_pause_min", (config.get("cycle_pause_seconds") or [0, 0])[0]))
    pause_max = int(updates.get("cycle_pause_max", (config.get("cycle_pause_seconds") or [0, 0])[1]))
    config["cycle_pause_seconds"] = [max(1, min(pause_min, pause_max)), max(1, max(pause_min, pause_max))]
    baron = dict(config.get("baron_attacks") or {})
    if "use_feathers" in updates:
        baron["use_feathers"] = bool(updates["use_feathers"])
    if "gold_fallback_when_no_feathers" in updates:
        baron["gold_fallback_when_no_feathers"] = bool(updates["gold_fallback_when_no_feathers"])
    config["baron_attacks"] = baron
    modes = dict(config.get("modes") or {})
    for key in ("barons", "nomads", "shogun"):
        if key in updates:
            modes[key] = bool(updates[key])
    config["modes"] = modes
    bluestacks = dict(config.get("bluestacks") or {})
    if updates.get("input") in {"mouse", "adb"}:
        bluestacks["input"] = updates["input"]
    config["bluestacks"] = bluestacks
    control = dict(config.get("control") or {})
    if "hotkey" in updates:
        control["hotkey"] = normalize_hotkey(str(updates["hotkey"]))
        CONTROL.set_hotkey(control["hotkey"])
    if "always_on_top" in updates:
        control["always_on_top"] = bool(updates["always_on_top"])
        CONTROL.always_on_top = control["always_on_top"]
    if "start_paused" in updates:
        control["start_paused"] = bool(updates["start_paused"])
    config["control"] = control
    # Never call CONTROL.configure() here: start_paused is startup-only.
    return public_settings(config)
