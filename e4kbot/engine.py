from __future__ import annotations

import time
from typing import Any

from loguru import logger

from e4kbot.bluestacks import AdbClient, diagnose_targeting, probe_bluestacks
from e4kbot.client import BlueStacksEngine
from e4kbot.control import CONTROL, BotPaused
from e4kbot.protocol import ProtocolEngine
from e4kbot.safety import wait_active_hours
from e4kbot.state import StateStore
from e4kbot.telegram_bot import TelegramReporter

BLUESTACKS_MISS_LIMIT = 5
BLUESTACKS_MISS_HINTS = (
    "нет скрина bluestacks",
    "окно bluestacks не найдено",
    "bluestacks не запущен",
    "окно bluestacks свёрнуто",
)
FAST_RETRY_RESULTS = {
    "no_targets",
    "unsafe_formation",
    "formation_not_found",
    "travel_dialog_not_found",
    "march_time_not_read",
    "feather_count_not_read",
}


class AttackBot:
    def __init__(self, config: dict[str, Any], store: StateStore, telegram: TelegramReporter) -> None:
        self.config = config
        self.store = store
        self.telegram = telegram
        self.adb = AdbClient(config)
        self.protocol: ProtocolEngine | None = None
        self.client: BlueStacksEngine | None = None
        self.stop = False
        self._bluestacks_misses = 0
        self._announced = False
        self._resume_now = False
        self._armed_for_report = False
        CONTROL.on_change(self._on_control_change)

    def start(self) -> None:
        self.store.reset_session_stats()
        self.store.live.running = True
        self.store.live.dry_run = bool(self.config.get("dry_run", True))
        self.store.live.engine = str(self.config.get("engine") or "bluestacks")
        self.store.live.account = "BlueStacks"
        self.store.live.stopped_reason = ""
        self.store.live.paused = not CONTROL.is_enabled()
        self.store.save()
        try:
            self.adb.connect()
        except Exception:
            logger.exception("ADB не подключился на старте — продолжаю, жду ВКЛ")
        diagnose_targeting(self.config, self.adb)
        logger.info("Бот готов. ВКЛ / клавиша {} запускает атаки", CONTROL.hotkey)
        try:
            self._loop()
        except BotPaused:
            logger.info("Подготовка прервана паузой — жду ВКЛ")
            try:
                self._loop()
            except Exception:
                logger.exception("Цикл бота упал")
        except Exception:
            logger.exception("Цикл бота упал")
            if not self.stop and not CONTROL.stop:
                try:
                    self._loop()
                except Exception:
                    logger.exception("Повторный цикл тоже упал")
        finally:
            self.store.live.running = False
            self.store.live.mode = "stopped"
            self.store.save()
            if self.protocol:
                self.protocol.close()

    def _loop(self) -> None:
        while not self.stop and not CONTROL.stop:
            try:
                if not CONTROL.is_enabled():
                    self.store.live.mode = "paused"
                    self.store.live.paused = True
                    self.store.save()
                    logger.info("На паузе — жми {} или кнопку ВКЛ", CONTROL.hotkey)
                    CONTROL.wait_until_enabled()
                    if self.stop or CONTROL.stop:
                        break
                    self._resume_now = True
                    self.store.live.paused = False
                    self.store.live.mode = "attack"
                    self.store.save()
                    logger.info("ВКЛ — сразу ищу цели")
                    continue

                wait_active_hours(self.config)
                if not self._ensure_bluestacks():
                    if self.stop:
                        break
                    try:
                        CONTROL.sleep(5)
                    except BotPaused:
                        continue
                    continue

                if not self._ensure_engines():
                    try:
                        CONTROL.sleep(3)
                    except BotPaused:
                        continue
                    continue

                if not self._announced:
                    self._announce()
                    self._announced = True

                skip_send_wait = self._resume_now
                self._resume_now = False
                self.store.prune()
                commander_wait = float(self.store.live.next_attack_at or 0)
                if not skip_send_wait and commander_wait > time.time():
                    wait_for = max(1.0, commander_wait - time.time())
                    self.store.live.mode = "wait_commanders"
                    self.store.save()
                    logger.info(f"Жду возврат военачальника {wait_for:.0f}с")
                    CONTROL.sleep(min(wait_for, 15))
                    continue
                in_flight = self.store.in_flight()
                cap = int(self.config.get("max_concurrent_attacks") or 30)
                if len(in_flight) >= cap:
                    nearest = min(m.return_at for m in in_flight)
                    wait_for = max(1.0, nearest - time.time())
                    self.store.live.mode = "wait_commanders"
                    self.store.live.next_attack_at = nearest
                    self.store.save()
                    logger.info(f"Лимит {cap} атак в пути, ждём возврат {wait_for:.0f}с")
                    CONTROL.sleep(min(wait_for, 15))
                    continue

                self.store.live.mode = "attack"
                self.store.live.paused = False
                self.store.save()
                if self.protocol:
                    result = self.protocol.run_cycle()
                elif self.client:
                    result = self.client.run_cycle()
                else:
                    result = "idle"
            except BotPaused:
                self.store.live.mode = "paused"
                self.store.live.paused = True
                self.store.save()
                logger.info("Пауза: клики остановлены, мышь свободна")
                continue
            except Exception as exc:
                self.store.live.last_error = str(exc)
                self.store.save()
                logger.exception("Ошибка цикла атаки")
                if self._is_bluestacks_error(exc):
                    if self.record_bluestacks_miss(str(exc)):
                        break
                    try:
                        CONTROL.sleep(5)
                    except BotPaused:
                        continue
                    continue
                self.telegram.report_status(f"⚠️ Ошибка цикла: {exc}")
                if "10012" in str(exc):
                    self.telegram.report_stop(
                        "Ошибка 10012: аккаунт уже в игре. "
                        "Оставь BlueStacks включённым, но выйди из персонажа, "
                        "либо поставь engine=bluestacks."
                    )
                    break
                try:
                    CONTROL.sleep(8)
                except BotPaused:
                    continue
                continue

            if result == "stop" or self.store.live.stopped_reason:
                logger.warning(self.store.live.stopped_reason or "stop")
                break
            max_cycles = int(self.config.get("max_cycles") or 0)
            if max_cycles and result not in {"wait_return", *FAST_RETRY_RESULTS}:
                completed = int(self.config.get("_completed_cycles") or 0) + 1
                self.config["_completed_cycles"] = completed
                logger.info(f"Прогон {completed}/{max_cycles}: {result}")
                if completed >= max_cycles:
                    logger.info("Лимит прогонов достигнут — останавливаюсь")
                    break
            if result == "no_commanders":
                self.handle_no_commanders_result()
                continue
            if str(result) in FAST_RETRY_RESULTS or str(result).startswith("movement_"):
                try:
                    CONTROL.sleep(0.4)
                except BotPaused:
                    continue
                continue

        self.store.live.running = False
        self.store.live.mode = "stopped"
        self.store.save()

    def _announce(self) -> None:
        engine_name = self.store.live.engine
        self.telegram.report_status(
            "🚀 Бот атак запущен\n"
            "Игру открываешь сам в BlueStacks — логин не нужен.\n"
            f"Режим: {engine_name} / {self.config.get('attack_style') or 'on_screen'}\n"
            f"DRY-RUN: {self.store.live.dry_run}\n"
            "Каденс: 8–10 сек от прошлой успешной отправки. "
            "Бароны до таблички «нет военачальников», потом красный крестик и ожидание возврата"
        )

    def _ensure_engines(self) -> bool:
        engine_name = self.store.live.engine
        if engine_name != "bluestacks":
            if not self.protocol:
                self.protocol = ProtocolEngine(self.config, self.store, self.telegram, self.adb)
                self.protocol.connect()
            return True
        if not self.adb.serial:
            self.adb.connect()
        if not self.adb.serial:
            logger.warning("Нет ADB. В BlueStacks: Settings → Advanced → Android Debug Bridge → Enable.")
            return False
        if not self.client:
            self.client = BlueStacksEngine(self.config, self.store, self.telegram, self.adb)
        return True

    def _ensure_bluestacks(self) -> bool:
        if not self.config.get("require_bluestacks", True):
            self._bluestacks_misses = 0
            return True
        status = probe_bluestacks(self.config, self.adb)
        if status == "ok":
            self._bluestacks_misses = 0
            return True
        self.record_bluestacks_miss(status)
        return False

    def _on_control_change(self) -> None:
        if CONTROL.is_enabled():
            self._armed_for_report = True
            return
        if self.stop or not self._armed_for_report:
            return
        self._armed_for_report = False
        self.report_user_stop_summary()

    def report_user_stop_summary(self) -> None:
        self._send_session_summary("Остановлен кнопкой / горячей клавишей")
        self.store.live.mode = "paused"
        self.store.save()

    def report_no_commanders_summary(self) -> None:
        self._send_session_summary(
            "Нет свободных военачальников — закрыл красным крестиком, жду возврат"
        )

    def handle_no_commanders_result(self) -> None:
        """Stop new attacks, report the session, wait for THIS bot's marches, then resume."""
        self.report_no_commanders_summary()
        wait_until = float(self.store.live.next_attack_at or 0)
        nearest = self.store.next_return_at()
        if nearest and nearest > time.time() and nearest > wait_until:
            wait_until = float(nearest)
            self.store.live.next_attack_at = wait_until
            self.store.live.mode = "wait_commanders"
            self.store.save()
        if wait_until > time.time():
            logger.info(
                "Нет военачальников — жду возврат {:.0f}с, потом продолжу если ВКЛ",
                wait_until - time.time(),
            )
            return
        logger.info("Нет военачальников и никто не в пути — пауза до ВКЛ")
        self._armed_for_report = False
        CONTROL.disable()

    def _send_session_summary(self, reason: str) -> None:
        summary = self.store.session_summary()
        logger.info(
            "Сводка сессии: атак {}, золото {}, рубины {}",
            summary["attacks"],
            summary["gold"],
            summary["rubies"],
        )
        self.telegram.report_shutdown_summary(
            summary["attacks"],
            summary["gold"],
            summary["rubies"],
            reason=reason,
        )

    def record_bluestacks_miss(self, reason: str) -> bool:
        """Count a consecutive BlueStacks miss. True if the bot should stop."""
        self._bluestacks_misses += 1
        logger.warning(
            "BlueStacks не виден ({}/{}): {}",
            self._bluestacks_misses,
            BLUESTACKS_MISS_LIMIT,
            reason,
        )
        if self._bluestacks_misses < BLUESTACKS_MISS_LIMIT:
            return False
        return self.shutdown_missing_bluestacks(reason)

    def shutdown_missing_bluestacks(self, reason: str = "") -> bool:
        summary = self.store.session_summary()
        text = (
            "BlueStacks не найден 5 раз подряд"
            + (f" ({reason})" if reason else "")
        )
        logger.error(text)
        self.store.live.stopped_reason = text
        self.stop = True
        CONTROL.disable()
        self.telegram.report_shutdown_summary(
            summary["attacks"],
            summary["gold"],
            summary["rubies"],
            reason=text,
        )
        self.store.save()
        return True

    @staticmethod
    def _is_bluestacks_error(exc: BaseException) -> bool:
        blob = str(exc).lower()
        return any(hint in blob for hint in BLUESTACKS_MISS_HINTS)
