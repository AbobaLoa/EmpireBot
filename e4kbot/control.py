from __future__ import annotations

import ctypes
import threading
import time
from typing import Any, Callable

from loguru import logger

from e4kbot.modes.catalog import catalog_grouped, catalog_payload


class BotPaused(Exception):
    """Raised when a click/wait must abort because the operator paused the bot."""


DEFAULT_HOTKEY = "NUM0"
DEFAULT_START_HOTKEY = "NUM1"
VK_NUMPAD0 = 0x60
VK_NUMPAD1 = 0x61
VK_INSERT = 0x2D
VK_END = 0x23
VK_NUMLOCK = 0x90
_KEY_DOWN = 0x8000

_NUMPAD0_ALIASES = {
    "NUM0",
    "NUMPAD0",
    "NUMPADINSERT",
    "NUMINSERT",
    "KP0",
    "KPINSERT",
}
_NUMPAD1_ALIASES = {
    "NUM1",
    "NUMPAD1",
    "NUMPADEND",
    "NUMEND",
    "KP1",
    "KPEND",
}


def normalize_hotkey(raw: str | None, default: str = DEFAULT_HOTKEY) -> str:
    text = str(raw or default).strip().upper()
    compact = text.replace(" ", "").replace("-", "").replace("_", "")
    if compact in _NUMPAD0_ALIASES:
        return DEFAULT_HOTKEY
    if compact in _NUMPAD1_ALIASES:
        return DEFAULT_START_HOTKEY
    if compact.startswith("F") and compact[1:].isdigit():
        number = int(compact[1:])
        if 1 <= number <= 12:
            return f"F{number}"
    if len(text) == 1 and ("A" <= text <= "Z" or "0" <= text <= "9"):
        return text
    return default


def normalize_start_hotkey(raw: str | None) -> str:
    key = normalize_hotkey(raw, default=DEFAULT_START_HOTKEY)
    if key == DEFAULT_HOTKEY:
        return DEFAULT_START_HOTKEY
    return key


def hotkey_label(hotkey: str | None = None) -> str:
    key = str(hotkey or DEFAULT_HOTKEY).strip().upper()
    compact = key.replace(" ", "").replace("-", "").replace("_", "")
    if compact in _NUMPAD0_ALIASES or key == DEFAULT_HOTKEY:
        return "Num0"
    if compact in _NUMPAD1_ALIASES or key == DEFAULT_START_HOTKEY:
        return "Num1"
    normalized = normalize_hotkey(hotkey)
    if normalized == DEFAULT_HOTKEY:
        return "Num0"
    if normalized == DEFAULT_START_HOTKEY:
        return "Num1"
    return normalized


def _numlock_on(user32: Any) -> bool:
    return bool(user32.GetKeyState(VK_NUMLOCK) & 1)


def _pause_key_down(user32: Any, hotkey: str) -> bool:
    """True while the pause bind is held. NUM0 works with NumLock on or off."""
    key = normalize_hotkey(hotkey)
    if key == DEFAULT_HOTKEY:
        if user32.GetAsyncKeyState(VK_NUMPAD0) & _KEY_DOWN:
            return True
        if not _numlock_on(user32) and user32.GetAsyncKeyState(VK_INSERT) & _KEY_DOWN:
            return True
        return False
    if key.startswith("F") and key[1:].isdigit():
        vk = 0x70 + int(key[1:]) - 1
    else:
        vk = ord(key)
    return bool(user32.GetAsyncKeyState(vk) & _KEY_DOWN)


def _start_key_down(user32: Any, hotkey: str) -> bool:
    """True while the start bind is held. NUM1 works with NumLock on or off."""
    key = normalize_start_hotkey(hotkey)
    if key == DEFAULT_START_HOTKEY:
        if user32.GetAsyncKeyState(VK_NUMPAD1) & _KEY_DOWN:
            return True
        if not _numlock_on(user32) and user32.GetAsyncKeyState(VK_END) & _KEY_DOWN:
            return True
        return False
    if key.startswith("F") and key[1:].isdigit():
        vk = 0x70 + int(key[1:]) - 1
    else:
        vk = ord(key)
    return bool(user32.GetAsyncKeyState(vk) & _KEY_DOWN)


class ControlBus:
    def __init__(self) -> None:
        self._enabled = threading.Event()
        self._enabled.set()
        self._lock = threading.Lock()
        self.stop = False
        self.hotkey = DEFAULT_HOTKEY
        self.start_hotkey = DEFAULT_START_HOTKEY
        self.always_on_top = True
        self._listeners: list[Callable[[], None]] = []
        self._hotkey_thread: threading.Thread | None = None
        self._hotkey_stop = threading.Event()
        self._hotkey_ready = threading.Event()
        self._hotkey_error = ""

    def configure(self, config: dict[str, Any], *, startup: bool = False) -> None:
        """Apply hotkeys/topmost. start_paused only pauses at process start, never later."""
        control = config.get("control") or {}
        self.hotkey = normalize_hotkey(str(control.get("hotkey") or DEFAULT_HOTKEY))
        self.start_hotkey = normalize_start_hotkey(
            str(control.get("start_hotkey") or DEFAULT_START_HOTKEY)
        )
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
            "hotkey_label": hotkey_label(self.hotkey),
            "start_hotkey": self.start_hotkey,
            "start_hotkey_label": hotkey_label(self.start_hotkey),
            "pause_hotkey": self.hotkey,
            "pause_hotkey_label": hotkey_label(self.hotkey),
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
            logger.info("Бот ВКЛ — клики разрешены (старт {})", hotkey_label(self.start_hotkey))
            self._notify()

    def disable(self) -> None:
        changed = self._enabled.is_set()
        self._enabled.clear()
        if changed:
            logger.info("Бот ВЫКЛ — мышь свободна (пауза {})", hotkey_label(self.hotkey))
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
        logger.info("Горячая клавиша паузы: {}", hotkey_label(self.hotkey))
        self._notify()
        return self.hotkey

    def set_start_hotkey(self, hotkey: str) -> str:
        self.start_hotkey = normalize_start_hotkey(hotkey)
        self.restart_hotkey()
        logger.info("Горячая клавиша старта: {}", hotkey_label(self.start_hotkey))
        self._notify()
        return self.start_hotkey

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
        user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
        user32.GetAsyncKeyState.restype = ctypes.c_short
        user32.GetKeyState.argtypes = [ctypes.c_int]
        user32.GetKeyState.restype = ctypes.c_short
        self._hotkey_ready.set()
        logger.info(
            "Глобальные хоткеи: старт {} (цифровая 1), пауза {} (цифровая 0). NumLock не важен, раскладка не важна.",
            hotkey_label(self.start_hotkey),
            hotkey_label(self.hotkey),
        )
        was_pause = False
        was_start = False
        while not self._hotkey_stop.is_set() and not self.stop:
            pause_down = _pause_key_down(user32, self.hotkey)
            start_down = _start_key_down(user32, self.start_hotkey)
            if pause_down and not was_pause:
                self.disable()
            elif start_down and not was_start:
                self.enable()
            was_pause = pause_down
            was_start = start_down
            time.sleep(0.02)


CONTROL = ControlBus()


def public_settings(config: dict[str, Any]) -> dict[str, Any]:
    from e4kbot.runtime.scheduler import campaign_queue, enabled_mode_ids, sync_legacy_modes

    delays = config.get("attack_delay_seconds") or [8, 10]
    cycles = config.get("cycle_pause_seconds") or [0, 0]
    baron = config.get("baron_attacks") or {}
    control = config.get("control") or {}
    sync_legacy_modes(config)
    modes = config.get("modes") or {}
    queue = campaign_queue(config)
    enabled = enabled_mode_ids(config)
    pause_key = normalize_hotkey(str(control.get("hotkey") or CONTROL.hotkey))
    start_key = normalize_start_hotkey(str(control.get("start_hotkey") or CONTROL.start_hotkey))
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
        "barons": bool(modes.get("barons", False)),
        "nomads": bool(modes.get("nomads", False)),
        "shogun": bool(modes.get("shogun", False)),
        "hotkey": pause_key,
        "hotkey_label": hotkey_label(pause_key),
        "start_hotkey": start_key,
        "start_hotkey_label": hotkey_label(start_key),
        "pause_hotkey": pause_key,
        "pause_hotkey_label": hotkey_label(pause_key),
        "always_on_top": bool(control.get("always_on_top", True)),
        "start_paused": bool(control.get("start_paused", True)),
        "input": str((config.get("bluestacks") or {}).get("input") or "mouse"),
        "campaign": queue,
        "enabled_modes": enabled,
        "catalog": catalog_payload(),
        "worlds": catalog_grouped(),
    }


def apply_public_settings(config: dict[str, Any], updates: dict[str, Any]) -> dict[str, Any]:
    from e4kbot.runtime.scheduler import apply_campaign_queue, sync_legacy_modes

    kind = str(updates.get("current_target_kind") or config.get("current_target_kind") or "baron")
    if kind == "shogun":
        kind = "samurai"
    config["current_target_kind"] = kind
    if "campaign" in updates or "enabled_modes" in updates:
        queue = updates.get("campaign")
        flags = updates.get("enabled_modes")
        if isinstance(queue, dict):
            queue = queue.get("queue") or queue.get("steps")
        apply_campaign_queue(
            config,
            queue if isinstance(queue, list) else None,
            flags if isinstance(flags, dict) else None,
        )
    else:
        legacy_flags = {}
        if "barons" in updates:
            legacy_flags["robber_barons"] = bool(updates["barons"])
        if "nomads" in updates:
            legacy_flags["nomad_camps"] = bool(updates["nomads"])
        if "shogun" in updates:
            legacy_flags["samurai_camps"] = bool(updates["shogun"])
        if legacy_flags:
            apply_campaign_queue(config, enabled_modes=legacy_flags)
        else:
            sync_legacy_modes(config)
    if "dry_run" in updates:
        config["dry_run"] = bool(updates["dry_run"])
    if "max_concurrent_attacks" in updates:
        config["max_concurrent_attacks"] = max(1, min(30, int(updates["max_concurrent_attacks"])))
    if "max_commander_number" in updates:
        config["max_commander_number"] = max(1, min(30, int(updates["max_commander_number"])))
    if "attack_delay_min" in updates or "attack_delay_max" in updates:
        delay_min = int(updates.get("attack_delay_min", (config.get("attack_delay_seconds") or [8, 10])[0]))
        delay_max = int(updates.get("attack_delay_max", (config.get("attack_delay_seconds") or [8, 10])[1]))
        config["attack_delay_seconds"] = [max(1, min(delay_min, delay_max)), max(1, max(delay_min, delay_max))]
    if "cycle_pause_min" in updates or "cycle_pause_max" in updates:
        pause_min = int(updates.get("cycle_pause_min", (config.get("cycle_pause_seconds") or [0, 0])[0]))
        pause_max = int(updates.get("cycle_pause_max", (config.get("cycle_pause_seconds") or [0, 0])[1]))
        config["cycle_pause_seconds"] = [max(0, min(pause_min, pause_max)), max(0, max(pause_min, pause_max))]
    baron = dict(config.get("baron_attacks") or {})
    if "use_feathers" in updates:
        baron["use_feathers"] = bool(updates["use_feathers"])
    if "gold_fallback_when_no_feathers" in updates:
        baron["gold_fallback_when_no_feathers"] = bool(updates["gold_fallback_when_no_feathers"])
    config["baron_attacks"] = baron
    bluestacks = dict(config.get("bluestacks") or {})
    if updates.get("input") in {"mouse", "adb"}:
        bluestacks["input"] = updates["input"]
    config["bluestacks"] = bluestacks
    control = dict(config.get("control") or {})
    if "hotkey" in updates:
        control["hotkey"] = normalize_hotkey(str(updates["hotkey"]))
        CONTROL.set_hotkey(control["hotkey"])
    if "start_hotkey" in updates:
        control["start_hotkey"] = normalize_start_hotkey(str(updates["start_hotkey"]))
        CONTROL.set_start_hotkey(control["start_hotkey"])
    if "always_on_top" in updates:
        control["always_on_top"] = bool(updates["always_on_top"])
        CONTROL.always_on_top = control["always_on_top"]
    if "start_paused" in updates:
        control["start_paused"] = bool(updates["start_paused"])
    control.setdefault("hotkey", CONTROL.hotkey)
    control.setdefault("start_hotkey", CONTROL.start_hotkey)
    config["control"] = control
    return public_settings(config)
