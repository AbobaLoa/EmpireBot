from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

from e4kbot.paths import CONFIG_PATH, DEFAULT_LEGACY_BOT

E4K_SERVERS: dict[str, tuple[str, str]] = {
    "ru1": (
        "tcp://e4k-live-ru1-game.goodgamestudios.com:443",
        "EmpirefourkingdomsExGG_10",
    ),
    "ru2": (
        "tcp://e4k-live-ru2-game.goodgamestudios.com:443",
        "EmpirefourkingdomsExGG_31",
    ),
    "de1": (
        "tcp://e4k-live-de1-game.goodgamestudios.com:443",
        "EmpirefourkingdomsExGG",
    ),
    "world1": (
        "tcp://e4k-live-world1-game.goodgamestudios.com:443",
        "EmpirefourkingdomsExGG_36",
    ),
}

DEFAULTS: dict[str, Any] = {
    "engine": "bluestacks",
    "dry_run": True,
    "attack_style": "on_screen",
    "current_target_kind": "baron",
    "live_api_allowed": False,
    "accept_risk": False,
    "server": "ru1",
    "platform": "mobile",
    "require_bluestacks": True,
    "require_game_running": False,
    "legacy_bot_path": str(DEFAULT_LEGACY_BOT),
    "max_concurrent_attacks": 30,
    "max_commander_number": 30,
    "attack_delay_seconds": [4, 10],
    "cycle_pause_seconds": [12, 25],
    "active_hours": [8, 24],
    "modes": {
        "barons": True,
        "nomads": True,
        "shogun": True,
        "prefer_events": True,
    },
    "baron_attacks": {
        "kingdom": 0,
        "npc_type": 2,
        "min_level": 1,
        "max_level": -1,
        "left_capacity": 100,
        "center_capacity": 150,
        "right_capacity": 100,
        "horses_type": -1,
        "use_feathers": True,
        "feathers_percent": 1,
        "gold_fallback_when_no_feathers": True,
    },
    "event_attacks": {
        "use_currency_boost": True,
        "min_tools_required": 1,
        "left_capacity": 122,
        "center_capacity": 122,
        "right_capacity": 122,
        "horses_type": -1,
        "use_feathers": True,
    },
    "bluestacks": {
        "adb_host": "127.0.0.1",
        "adb_ports": [5555, 5556, 5565],
        "adb_path": "",
        "window_title_hints": ["BlueStacks", "HD-Player", "Empire"],
        "package": "air.com.goodgamestudios.empirefourkingdoms",
        "layout": "default",
        "center_jitter": 0.06,
    },
    "vision": {
        "robber_threshold": 0.65,
        "screen_timeout_seconds": 8,
        "popup_retries": 4,
        "map_anchor": [0.50, 0.54],
        "map_coordinate_scale": [0.044, 0.044],
        "picker_timeout_seconds": 15,
        "picker_max_actions": 4,
        "minimum_flank_fill": 0.70,
    },
    "telegram": {
        "enabled": False,
        "bot_token": "",
        "chat_id": "",
        "miniapp_host": "127.0.0.1",
        "miniapp_port": 8766,
        "public_webapp_url": "",
    },
    "accounts": [],
}


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def load_config(path: Path | None = None) -> dict[str, Any]:
    config_path = path or CONFIG_PATH
    raw: dict[str, Any] = {}
    if config_path.exists():
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    return deep_merge(DEFAULTS, raw)


def save_config(config: dict[str, Any], path: Path | None = None) -> None:
    config_path = path or CONFIG_PATH
    config_path.write_text(
        json.dumps(config, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def server_endpoint(config: dict[str, Any]) -> tuple[str, str]:
    server = str(config.get("server") or "ru1")
    if server not in E4K_SERVERS:
        raise ValueError(f"Неизвестный сервер: {server}")
    return E4K_SERVERS[server]


def enabled_account(config: dict[str, Any]) -> dict[str, Any]:
    for account in config.get("accounts") or []:
        if account.get("enabled", True) and account.get("username"):
            return account
    raise RuntimeError("В config.json нет включённого аккаунта")
