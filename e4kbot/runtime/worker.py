from __future__ import annotations

import json
import sys
import threading
from typing import Any

from loguru import logger

from e4kbot.config import load_config, save_config
from e4kbot.control import CONTROL
from e4kbot.engine import AttackBot
from e4kbot.modes.catalog import catalog_payload
from e4kbot.paths import ROOT, ensure_dirs
from e4kbot.runtime.live import emit, emit_ready, emit_state, install_worker_sink
from e4kbot.runtime.scheduler import snapshot
from e4kbot.state import StateStore
from e4kbot.telegram_bot import TelegramReporter


class Worker:
    def __init__(self) -> None:
        ensure_dirs()
        self.config = load_config()
        CONTROL.configure(self.config, startup=True)
        self.store = StateStore()
        self.telegram = TelegramReporter(self.config.get("telegram") or {})
        self.bot = AttackBot(self.config, self.store, self.telegram)
        self._thread: threading.Thread | None = None
        threading.Thread(target=self._pulse, name="e4k-pulse", daemon=True).start()

    def _pulse(self) -> None:
        import time

        while True:
            try:
                self.status()
            except Exception:
                emit("worker.pulse_failed", level="ERROR")
            time.sleep(2)

    def start_bot(self) -> None:
        if self._thread and self._thread.is_alive():
            CONTROL.enable()
            emit("worker.resume")
            return
        self.bot.stop = False
        CONTROL.stop = False
        self._thread = threading.Thread(target=self.bot.start, name="e4k-engine", daemon=True)
        self._thread.start()
        emit("worker.start")

    def handle(self, command: dict[str, Any]) -> dict[str, Any]:
        cmd = str(command.get("cmd") or "")
        if cmd in {"start", "resume"}:
            self.start_bot()
            CONTROL.enable()
            return {"ok": True, "cmd": cmd}
        if cmd == "pause":
            CONTROL.disable()
            emit("worker.pause")
            return {"ok": True, "cmd": cmd}
        if cmd == "stop":
            self.bot.stop = True
            CONTROL.shutdown()
            emit("worker.stop")
            return {"ok": True, "cmd": cmd}
        if cmd == "set_dry_run":
            value = bool(command.get("value", True))
            self.config["dry_run"] = value
            self.store.live.dry_run = value
            self.store.save()
            save_config(self.config)
            emit("config.dry_run", value=value)
            return {"ok": True, "dry_run": value}
        if cmd == "set_campaign":
            queue = command.get("queue") or []
            campaign = self.config.setdefault("campaign", {})
            campaign["queue"] = queue
            save_config(self.config)
            emit("config.campaign", queue=queue)
            return {"ok": True, "campaign": snapshot(self.config, self.store)}
        if cmd == "status":
            return self.status()
        emit("worker.unknown_cmd", cmd=cmd)
        return {"ok": False, "error": f"unknown_cmd:{cmd}"}

    def status(self) -> dict[str, Any]:
        payload = self.store.live.to_dict()
        payload["campaign"] = snapshot(self.config, self.store)
        payload["catalog"] = catalog_payload()
        payload["root"] = str(ROOT)
        emit_state(payload)
        return payload


def main() -> None:
    install_worker_sink()
    worker = Worker()
    emit_ready(
        {
            "root": str(ROOT),
            "catalog": catalog_payload(),
            "campaign": snapshot(worker.config, worker.store),
            "dry_run": bool(worker.config.get("dry_run", True)),
        }
    )
    emit("worker.waiting_commands")
    for raw in sys.stdin:
        line = raw.strip()
        if not line:
            continue
        try:
            command = json.loads(line)
        except json.JSONDecodeError as exc:
            emit("worker.bad_json", level="ERROR", error=str(exc), line=line[:200])
            continue
        try:
            result = worker.handle(command)
            print(json.dumps({"type": "ack", "payload": result}, ensure_ascii=False), flush=True)
        except Exception as exc:
            logger.exception("Команда воркера упала")
            emit("worker.command_failed", level="ERROR", error=str(exc))


if __name__ == "__main__":
    main()
