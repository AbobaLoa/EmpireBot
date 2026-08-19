from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
LOG_DIR = ROOT / "logs"
SHOTS_DIR = ROOT / "shots"
WEBAPP_DIR = ROOT / "webapp"
LAYOUTS_DIR = ROOT / "layouts"
CONFIG_PATH = ROOT / "config.json"
STATE_PATH = DATA_DIR / "state.json"

DEFAULT_LEGACY_BOT = Path(r"C:\Users\Dima\Desktop\EFK-Sabotage-Bot")


def ensure_dirs() -> None:
    for path in (DATA_DIR, LOG_DIR, SHOTS_DIR, LAYOUTS_DIR):
        path.mkdir(parents=True, exist_ok=True)


def add_legacy_bot_path(legacy_path: Path | None = None) -> Path:
    path = Path(legacy_path or DEFAULT_LEGACY_BOT)
    resolved = str(path.resolve())
    if resolved not in sys.path:
        sys.path.insert(0, resolved)
    return path
