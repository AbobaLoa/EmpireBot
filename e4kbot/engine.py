from __future__ import annotations

import time
from typing import Any

from loguru import logger

from e4kbot.attacks.registry import get_attack_module
from e4kbot.bluestacks import AdbClient, diagnose_targeting, probe_bluestacks
from e4kbot.campaign_checkpoint import (
    apply_on_enable,
    apply_on_start,
    mark_paused,
    restore_into_client,
    write_checkpoint,
)
from e4kbot.client import BlueStacksEngine
from e4kbot.control import CONTROL, BotPaused, hotkey_label
from e4kbot.protocol import ProtocolEngine
from e4kbot.runtime.live import emit, emit_state
from e4kbot.runtime.scheduler import pick_next_step, snapshot, steps
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
    "campaign_complete",
    "map_loading",
    "retry_samurai_tools",
    "retry_nomad_tools",
    "autoselect_failed",
    "samurai_complete",
    "nomad_complete",
    "waiting_camp_template",
    "world_unopened",
    "world_skip_empty",
    "world_switch_failed",
    "nav_not_found",
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
        decision = apply_on_start(self.store, self.client, config=self.config)
        if decision == "restart" and bool(self.config.get("resume_session_stats")):
            logger.info("resume_session_stats в конфиге, но чекпоинт старше 10 мин — всё равно старт с Великой империи")
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
        logger.info(
            "Бот готов. Старт {} включает выбранные задачи, пауза {} отпускает мышь",
            hotkey_label(CONTROL.start_hotkey),
            hotkey_label(CONTROL.hotkey),
        )
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
            if self._armed_for_report:
                try:
                    mark_paused(self.store, self.client)
                except Exception:
                    logger.exception("Не удалось записать чекпоинт паузы при выходе")
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
                    logger.info(
                        "На паузе — жми {} чтобы стартовать выбранные задачи",
                        hotkey_label(CONTROL.start_hotkey),
                    )
                    CONTROL.wait_until_enabled()
                    if self.stop or CONTROL.stop:
                        break
                    self._resume_now = True
                    self.store.live.paused = False
                    self.store.live.mode = "attack"
                    self.store.save()
                    logger.info("ВКЛ — проверяю экран и продолжаю с чекпоинта или с Великой империи")
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
                self.store.live.mode = "attack"
                self.store.live.paused = False
                self.store.save()
                result = self._run_scheduled_cycle()
                write_checkpoint(self.store, self.client)
            except BotPaused:
                mark_paused(self.store, self.client)
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
            if str(result).startswith("stub:"):
                emit("cycle.stub_skip", result=result)
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

    def _run_scheduled_cycle(self) -> str:
        campaign = self.config.get("campaign") or {}
        if campaign.get("enabled", True):
            step = pick_next_step(self.config, self.store)
            pinned_id = str(self.store.live.active_mode or "")
            if step is not None and self.client is not None and pinned_id:
                try:
                    assembling = bool(self.client._plan_or_picker_open())
                except Exception:
                    assembling = False
                if assembling:
                    hold = next(
                        (
                            item
                            for item in steps(self.config, self.store)
                            if item.mode_id == pinned_id and item.enabled and not item.done
                        ),
                        None,
                    )
                    if hold is not None:
                        step = hold
            emit(
                "cycle.next",
                campaign=snapshot(self.config, self.store),
                in_flight=len(self.store.in_flight()),
            )
            if step is None:
                emit("campaign.complete", level="INFO")
                self.store.live.last_action = "нет включённых задач"
                logger.info("Нет включённых задач — включи тумблеры и нажми {}", hotkey_label(CONTROL.start_hotkey))
                emit_state(self.store.live.to_dict())
                return "campaign_complete"
            self.store.live.active_mode = step.mode_id
            self.store.live.last_action = f"{step.spec.title_ru} · поиск"
            self.config["current_target_kind"] = step.spec.target_kind
            tier = int(step.spec.campaign_priority or 0)
            if tier:
                logger.info("Очередь: P{} {} (выключенные тумблеры пропускаю)", tier, step.spec.title_ru)
            emit(
                "mode.select",
                mode=step.mode_id,
                official_name=step.spec.official_name,
                remaining=step.remaining,
                status=step.spec.status,
            )
            if step.spec.status == "stub":
                emit(
                    "mode.stub",
                    level="WARNING",
                    mode=step.mode_id,
                    official_name=step.spec.official_name,
                    reason="not_implemented",
                )
                logger.warning(
                    "Режим «{}» ({}) — заглушка, реализация позже",
                    step.spec.title_ru,
                    step.spec.official_name,
                )
                self.store.skip_mode(step.mode_id)
                emit_state(self.store.live.to_dict())
                return f"stub:{step.mode_id}"
        if self.protocol:
            result = self.protocol.run_cycle()
        elif self.client:
            if self.client.wait_out_loading():
                result = "map_loading"
            else:
                mode_id = self.store.live.active_mode
                if not mode_id:
                    kind = str(self.config.get("current_target_kind") or "baron")
                    mode_id = {
                        "samurai": "samurai_camps",
                        "nomad": "nomad_camps",
                        "baron": "robber_barons",
                        "barbarian_tower": "barbarian_towers",
                        "desert_tower": "desert_towers",
                        "cultist_tower": "cultist_towers",
                        "storm_fort": "storm_forts",
                    }.get(kind, kind)
                result = get_attack_module(mode_id).run_cycle(self.client)
        else:
            emit("cycle.idle", level="WARNING")
            result = "idle"
        emit("cycle.result", result=result, mode=self.store.live.active_mode)
        coords = self.store.live.last_coords or "—"
        mode_label = self.store.live.active_mode or "idle"
        self.store.live.last_action = f"{mode_label} · {result} · {coords}"
        emit_state(self.store.live.to_dict())
        return result

    def _announce(self) -> None:
        engine_name = self.store.live.engine
        self.telegram.report_status(
            "🚀 Бот атак запущен\n"
            "Игру открываешь сам в BlueStacks — логин не нужен.\n"
            f"Режим: {engine_name} / {self.config.get('attack_style') or 'on_screen'}\n"
            f"DRY-RUN: {self.store.live.dry_run}\n"
            "Каденс: 8–10 сек от прошлой успешной отправки. "
            "Новые атаки пока не появится надпись «нет свободных военачальников», "
            "потом красный крестик (не нанимать за рубины) и ожидание возврата. "
            "Квота мира не останавливает бота — только Num0 / выкл."
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
            from e4kbot.campaign_checkpoint import load_checkpoint, should_resume

            payload = load_checkpoint()
            if should_resume(payload):
                restore_into_client(self.client, payload)
            elif getattr(self.store.live, "active_mode", "") in {
                "robber_barons",
                "nomad_camps",
                "samurai_camps",
                "alien_castles",
            }:
                self.client._need_ge_home = True
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
        client = getattr(self, "client", None)
        if CONTROL.is_enabled():
            self._armed_for_report = True
            apply_on_enable(self.store, client, config=getattr(self, "config", None))
            return
        if self.stop or not self._armed_for_report:
            return
        self._armed_for_report = False
        mark_paused(self.store, client)
        self.report_user_stop_summary()

    def report_user_stop_summary(self) -> None:
        self._send_session_summary("Остановлен кнопкой / горячей клавишей")
        self.store.live.mode = "paused"
        self.store.save()

    def report_no_commanders_summary(self) -> None:
        self._send_session_summary(
            "Нет свободных военачальников/наместников — закрыл красным крестиком, жду возврат"
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
        fallback = time.time() + 12 * 60
        self.store.live.next_attack_at = fallback
        self.store.live.mode = "wait_commanders"
        self.store.save()
        logger.info(
            "Нет свободных наместников, локального таймера нет — жду 12 мин и продолжу"
        )

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
