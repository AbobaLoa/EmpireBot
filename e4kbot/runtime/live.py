from __future__ import annotations

import json
import sys
import time
from typing import Any

from loguru import logger

from e4kbot.paths import DATA_DIR, ensure_dirs

WORKER_MODE = False
LIVE_PATH = DATA_DIR / "live.jsonl"


def _write_jsonl(payload: dict[str, Any]) -> None:
    ensure_dirs()
    with LIVE_PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


def emit(event: str, level: str = "INFO", **fields: Any) -> dict[str, Any]:
    payload = {
        "type": "log",
        "ts": time.time(),
        "level": str(level).upper(),
        "event": event,
        "data": {key: value for key, value in fields.items() if value is not None},
    }
    try:
        _write_jsonl(payload)
    except Exception:
        pass
    if WORKER_MODE:
        print(json.dumps(payload, ensure_ascii=False), flush=True)
    logger.log(str(level).upper(), "{} {}", event, payload["data"])
    return payload


def emit_state(state: dict[str, Any]) -> None:
    payload = {"type": "state", "ts": time.time(), "payload": state}
    if WORKER_MODE:
        print(json.dumps(payload, ensure_ascii=False), flush=True)


def emit_ready(meta: dict[str, Any] | None = None) -> None:
    payload = {"type": "ready", "ts": time.time(), "payload": meta or {}}
    if WORKER_MODE:
        print(json.dumps(payload, ensure_ascii=False), flush=True)


def install_worker_sink() -> None:
    global WORKER_MODE
    WORKER_MODE = True

    def _sink(message: Any) -> None:
        record = message.record
        extra = dict(record["extra"] or {})
        payload = {
            "type": "log",
            "ts": record["time"].timestamp(),
            "level": record["level"].name,
            "event": extra.get("event") or "loguru",
            "logger": record["name"],
            "function": record["function"],
            "line": record["line"],
            "message": record["message"],
            "data": extra,
        }
        print(json.dumps(payload, ensure_ascii=False), flush=True)

    logger.add(_sink, level="TRACE", enqueue=False)
    logger.add(sys.stderr, level="DEBUG")
