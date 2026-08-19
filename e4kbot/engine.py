from __future__ import annotations

import time
from typing import Any

from loguru import logger

from e4kbot.bluestacks import AdbClient, bluestacks_running, wait_until_ready
from e4kbot.client import BlueStacksEngine
from e4kbot.protocol import ProtocolEngine
from e4kbot.safety import cycle_pause_sleep, wait_active_hours
from e4kbot.state import StateStore
from e4kbot.telegram_bot import TelegramReporter


class AttackBot:
    def __init__(self, config: dict[str, Any], store: StateStore, telegram: TelegramReporter) -> None:
        self.config = config
        self.store = store
        self.telegram = telegram
        self.adb = AdbClient(config)
        self.protocol: ProtocolEngine | None = None
        self.client: BlueStacksEngine | None = None
        self.stop = False

    def start(self) -> None:
        self.store.live.running = True
        self.store.live.dry_run = bool(self.config.get("dry_run", True))
        self.store.live.engine = str(self.config.get("engine") or "bluestacks")
        self.store.live.account = "BlueStacks"
        self.store.live.stopped_reason = ""
        self.store.save()

        if self.config.get("require_bluestacks", True) and not bluestacks_running():
            self.telegram.report_status("⏳ Жду BlueStacks (HD-Player)...")
        self.adb.connect()
        wait_until_ready(self.config, self.adb)

        engine_name = self.store.live.engine
        if engine_name != "bluestacks":
            self.protocol = ProtocolEngine(self.config, self.store, self.telegram, self.adb)
            self.protocol.connect()
        else:
            if not self.adb.serial:
                raise RuntimeError(
                    "Нет ADB. В BlueStacks: Settings → Advanced → Android Debug Bridge → Enable."
                )
            self.client = BlueStacksEngine(self.config, self.store, self.telegram, self.adb)

        self.telegram.report_status(
            "🚀 Бот атак запущен\n"
            "Игру открываешь сам в BlueStacks — логин не нужен.\n"
            f"Режим: {engine_name} / {self.config.get('attack_style') or 'on_screen'}\n"
            f"DRY-RUN: {self.store.live.dry_run}\n"
            "КД 4–10 с · лимит 30 атак · военачальник № ≤ 30"
        )
        self._loop()

    def _loop(self) -> None:
        while not self.stop:
            wait_active_hours(self.config)
            if self.config.get("require_bluestacks", True):
                wait_until_ready(self.config, self.adb)

            self.store.prune()
            in_flight = self.store.in_flight()
            cap = int(self.config.get("max_concurrent_attacks") or 30)
            if len(in_flight) >= cap:
                nearest = min(m.return_at for m in in_flight)
                wait_for = max(1.0, nearest - time.time())
                self.store.live.mode = "wait_commanders"
                self.store.live.next_attack_at = nearest
                self.store.save()
                logger.info(f"Лимит {cap} атак в пути, ждём возврат {wait_for:.0f}с")
                time.sleep(min(wait_for, 15))
                continue

            try:
                self.store.live.mode = "attack"
                self.store.save()
                if self.protocol:
                    result = self.protocol.run_cycle()
                elif self.client:
                    result = self.client.run_cycle()
                else:
                    result = "idle"
            except Exception as exc:
                self.store.live.last_error = str(exc)
                self.store.save()
                logger.exception("Ошибка цикла атаки")
                self.telegram.report_status(f"⚠️ Ошибка цикла: {exc}")
                if "10012" in str(exc):
                    self.telegram.report_stop(
                        "Ошибка 10012: аккаунт уже в игре. "
                        "Оставь BlueStacks включённым, но выйди из персонажа, "
                        "либо поставь engine=bluestacks."
                    )
                    break
                time.sleep(20)
                continue

            if result == "stop" or self.store.live.stopped_reason:
                logger.warning(self.store.live.stopped_reason or "stop")
                break
            self.store.live.mode = "pause"
            self.store.save()
            cycle_pause_sleep(self.config)

        self.store.live.running = False
        self.store.live.mode = "stopped"
        self.store.save()
        if self.protocol:
            self.protocol.close()
