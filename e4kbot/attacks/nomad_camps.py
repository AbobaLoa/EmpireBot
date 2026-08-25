from __future__ import annotations

import json
import re
from typing import Any

from loguru import logger

from e4kbot.bluestacks import save_shot
from e4kbot.control import CONTROL
from e4kbot.safety import concurrent_ok
from e4kbot.client import HuntTarget
from e4kbot.vision import (
    NOMAD_TEMPLATE,
    NomadToolStock,
    assign_flank_tools,
    crop_rel,
    find_apply_preset_all,
    find_autoselect_button,
    find_autoselect_confirm,
    find_formation_attack_button,
    find_messages_nav,
    find_nomad_tool_inventory,
    find_picker_max_control,
    find_preset_button,
    find_preset_dialog_close,
    find_red_cross_force,
    find_reward_confirm,
    find_target_attack_button,
    find_tool_slider_plus,
    is_autoselect_dialog,
    is_difficulty_dialog,
    is_event_reward_popup,
    is_formation_screen,
    is_inbox_screen,
    is_map_screen,
    is_no_commanders_parchment,
    is_overview_plaque,
    is_presets_dialog,
    is_ruby_shop,
    is_special_offers_screen,
    is_travel_dialog,
    find_travel_seal_pair,
    movement_confirm_diagnostics,
    ocr_text,
    ocr_text_ui,
    parse_count,
    parse_report_resources,
    parse_samurai_camp_level,
    pick_open_difficulty_point,
)

from e4kbot.state import NOMAD_HITS_PER_CAMP

NOMAD_CAMP_LIMIT = NOMAD_HITS_PER_CAMP
NOMAD_MAP_CAP = 4
NOMAD_SESSION_QUOTA = 44
FRESH_CAMP_REMAINING = NOMAD_HITS_PER_CAMP
INVENTORY_PAGES = 8
INVENTORY_STRIDE = 0.04


class NomadCampsModule:
    """Nomad Invasion: badge-tool inventory, different flanks, 11 hits per camp, 4 camps."""

    spec_id = "nomad_camps"
    is_stub = False

    def __init__(self) -> None:
        self._preset_ready = False
        self._difficulty_chosen_by_bot = False
        self._difficulty_checked = False
        self._levels_probed = False
        self._tools_unavailable = False
        self._planned_tools: list[NomadToolStock | None] = []
        self._following_next_camp = False
        self._eleven_reported = False

    def run_cycle(self, driver: Any | None = None) -> str:
        if driver is None:
            return "idle"
        if not NOMAD_TEMPLATE.exists():
            logger.warning("Нет assets/nomad_camp.png — охоту не начинаю, жду шаблон лагеря")
            return "waiting_camp_template"
        target_level = driver.prepare_nomad_level_cycle()
        if target_level is None:
            logger.info("Все уровни кочевников в диапазоне пройдены")
            return "nomad_levels_complete"
        logger.info("Кочевники: цикл охоты, пресет={}, целевой уровень {}", self._preset_ready, target_level)
        if bool(driver.config.get("nomad_preset_ready")):
            self._preset_ready = True
        ok, _ = concurrent_ok(len(driver.store.in_flight()), driver.config)
        if not ok:
            return "wait_return"
        if driver.wait_out_loading():
            return "map_loading"
        sent = int((driver.store.live.session_by_mode or {}).get(self.spec_id) or 0)
        if sent >= NOMAD_SESSION_QUOTA:
            return self._finish_event(driver, sent)
        if getattr(driver, "_hunt_queue", None) is None:
            driver._hunt_queue = []
        if getattr(driver, "_blocked_screen_targets", None) is None:
            driver._blocked_screen_targets = []
        current = driver._image()
        if is_difficulty_dialog(current):
            self._pick_difficulty(driver)
            current = driver._image()
        if driver._dismiss_no_commanders(current):
            return "no_commanders"
        current = driver._image()
        plain_map = (
            is_map_screen(current)
            and find_reward_confirm(current) is None
            and not is_special_offers_screen(current)
            and not is_overview_plaque(current)
            and not is_ruby_shop(current)
        )
        if not plain_map:
            driver._dismiss_connection_error_if_open(current)
            driver._dismiss_ruby_shop_if_open(current)
            driver._dismiss_special_offers_if_open(current)
            driver._dismiss_overview_if_open(current)
            driver._dismiss_reward_popups()
            driver._dismiss_hire_menu_if_open()
            driver._dismiss_inbox_if_open()
            driver._dismiss_blocking_menu_if_no_camps()
            driver._dismiss_taxes_if_open()
        current = driver._image()
        resumed = self._resume_open_plan(driver, current)
        if resumed is not None:
            return resumed
        self._restore_farm_queue(driver)
        if not driver._hunt_queue:
            driver._hunt_queue = driver._collect_hunt_batch("nomad")[:NOMAD_MAP_CAP]
            if not driver._hunt_queue:
                logger.info("На карте нет лагерей кочевников")
                return "no_targets"
            logger.info("Кочевники: {} лагерей рядом, дальше по списку", len(driver._hunt_queue))
            if not self._difficulty_chosen_by_bot:
                self._probe_camp_levels(driver)
        while driver._hunt_queue:
            current = driver._image()
            if is_difficulty_dialog(current):
                self._pick_difficulty(driver)
                current = driver._image()
            plain_map = (
                is_map_screen(current)
                and find_reward_confirm(current) is None
                and not is_special_offers_screen(current)
                and not is_overview_plaque(current)
                and not is_ruby_shop(current)
            )
            if not plain_map:
                driver._dismiss_ruby_shop_if_open(current)
                driver._dismiss_special_offers_if_open(current)
                driver._dismiss_overview_if_open(current)
                driver._dismiss_reward_popups()
                driver._dismiss_hire_menu_if_open()
                driver._dismiss_inbox_if_open()
                driver._dismiss_taxes_if_open()
            current = driver._image()
            resumed = self._resume_open_plan(driver, current)
            if resumed is not None:
                return resumed
            target = driver._hunt_queue[0]
            if target.coords:
                snapped = driver.store.canonicalize_nomad_coords(target.coords)
                if snapped and snapped != target.coords:
                    target = HuntTarget(target.point, snapped)
                    driver._hunt_queue[0] = target
            if not driver.store.camp_has_nomad_budget(target.coords):
                logger.info(
                    "Лагерь {} без оставшихся атак — следующий",
                    target.coords or target.point,
                )
                driver._hunt_queue.pop(0)
                continue
            point = driver._focus_hunt_target("nomad", target)
            if point is None:
                logger.info(
                    "Лагерь {} не на экране — очередь не сбрасываю, чужой не бью",
                    target.coords or target.point,
                )
                return "camp_offscreen"
            driver._last_nomad_point = point
            if target.coords:
                driver._selected_target_coords = target.coords
                driver.store.live.last_coords = f"K0 ({target.coords[0]}, {target.coords[1]})"
                driver.store.set_nomad_farm(target.coords, point)
            if driver._open_formation(point, "nomad"):
                self._maybe_pick_difficulty_after_attack(driver)
                return self._execute(driver, point)
            if getattr(driver, "_no_commanders_seen", False):
                return "no_commanders"
            current = driver._image()
            resumed = self._resume_open_plan(driver, current)
            if resumed is not None:
                return resumed
            logger.info(
                "Нападение не открылось — лагерь {} оставляю в очереди, чужой не беру",
                target.coords or target.point,
            )
            driver._nomad_recenter_next = True
            return "plaque_miss"
        return "no_targets"

    def _resume_open_plan(self, driver: Any, current: Any) -> str | None:
        if is_event_reward_popup(current):
            driver._dismiss_reward_popups(current)
            return "popup_dismissed"
        if is_special_offers_screen(current):
            driver._dismiss_special_offers_if_open(current)
            return "popup_dismissed"
        if is_ruby_shop(current):
            driver._dismiss_ruby_shop_if_open(current)
            return "popup_dismissed"
        near = getattr(driver, "_last_nomad_point", None)
        radial = find_target_attack_button(current, near=near)
        if (
            radial is not None
            and is_map_screen(current)
            and not is_special_offers_screen(current)
            and not is_ruby_shop(current)
        ):
            self._restore_selected_coords(driver)
            logger.info(
                "Радиал Напасть ({:.3f}, {:.3f}) уже открыт — жму атаку, не Обзор",
                radial[0],
                radial[1],
            )
            driver._tap_norm(*radial)
            opened = driver._wait_for(
                lambda img: is_difficulty_dialog(img)
                or is_formation_screen(img)
                or is_no_commanders_parchment(img)
                or is_travel_dialog(img),
                timeout=6,
                label="план после радиала Напасть",
            )
            if opened is None:
                return "radial_attack_missed"
            if driver._dismiss_no_commanders(opened):
                return "no_commanders"
            return self._execute(driver, radial)
        if is_formation_screen(current):
            self._restore_selected_coords(driver)
            logger.info("План кочевников уже открыт — продолжаю орудия/предустановки")
            return self._execute(driver, (0.50, 0.50))
        pair = find_travel_seal_pair(current)
        if pair is not None:
            green, _red = pair
            self._restore_selected_coords(driver)
            logger.info(
                "Галочка «Начать нападение» ({:.3f}, {:.3f}) — дальше план, не отправка",
                green[0],
                green[1],
            )
            driver._tap_norm_exact(*green)
            opened = driver._wait_for(is_formation_screen, timeout=8, label="план после галочки")
            if opened is None:
                return "formation_not_found"
            return self._execute(driver, (0.50, 0.50))
        if is_travel_dialog(current):
            diagnostic = movement_confirm_diagnostics(current)
            if diagnostic.get("valid"):
                self._restore_selected_coords(driver)
                logger.info("Диалог похода уже открыт — подтверждаю отправку")
                return self._execute(driver, (0.50, 0.50))
        if driver._plan_or_picker_open(current) and not is_map_screen(current):
            logger.info("Пикер без плана атаки — закрываю крестиком, не магазин")
            self._dismiss_tool_picker(driver)
            latest = driver._image()
            if is_formation_screen(latest):
                return self._execute(driver, (0.50, 0.50))
        return None

    def _restore_selected_coords(self, driver: Any) -> None:
        if driver._selected_target_coords:
            return
        match = re.search(
            r"\((\d+)\s*,\s*(\d+)\)",
            str(driver.store.live.last_coords or ""),
        )
        if match:
            driver._selected_target_coords = (
                int(match.group(1)),
                int(match.group(2)),
            )

    def _execute(self, driver: Any, point: tuple[float, float]) -> str:
        if driver._dismiss_no_commanders():
            return "no_commanders"
        current = driver._image()
        pair = find_travel_seal_pair(current)
        if pair is not None and not is_formation_screen(current):
            green, _red = pair
            logger.info(
                "Галочка «Начать нападение» ({:.3f}, {:.3f}) — открываю план",
                green[0],
                green[1],
            )
            driver._tap_norm_exact(*green)
            opened = driver._wait_for(is_formation_screen, timeout=8, label="план после галочки")
            if opened is None:
                return "formation_not_found"
            current = opened
        travel_already = (
            not is_formation_screen(current)
            and find_travel_seal_pair(current) is None
            and bool(movement_confirm_diagnostics(current).get("valid"))
        )
        if not travel_already:
            self._maybe_pick_difficulty_after_attack(driver)
            if not is_formation_screen(driver._image()):
                formation = driver._wait_for(is_formation_screen, timeout=10, label="план кочевников")
                if formation is None:
                    logger.warning("Нет плана атаки кочевников")
                    return "formation_not_found"
            prepared, reason = self._prepare_waves(driver)
            if not prepared:
                driver.store.live.last_error = reason
                driver.store.save()
                logger.warning("Кочевники: подготовка не прошла ({}) — план не закрываю зелёной печатью", reason)
                return reason
            driver._tap_formation_attack()
            travel = driver._wait_for(
                lambda img: is_travel_dialog(img) or is_no_commanders_parchment(img),
                timeout=driver._vision_seconds("travel_dialog_timeout_seconds", 15),
                label="диалог похода кочевников",
            )
            if travel is not None and driver._dismiss_no_commanders(travel):
                return "no_commanders"
            if travel is None:
                if driver._dismiss_no_commanders():
                    return "no_commanders"
                logger.warning("Нет диалога похода кочевников")
                return "travel_dialog_not_found"
        travel = driver._image()
        if find_travel_seal_pair(travel) is not None:
            logger.info("Компактный диалог — перья не трогаю")
            movement = "gold"
        else:
            movement, feathers = driver._movement_option(travel)
            if movement == "unknown":
                logger.info("Перья не прочитались — подтверждаю поход как есть, не отменяю")
                movement = "gold"
            else:
                CONTROL.sleep(0.4)
                travel = driver._image()
        one_way = driver._read_march_time(travel)
        if one_way is None:
            one_way = 30
            logger.info("Время похода не прочиталось — беру 30с, диалог не закрываю")
        fake_target = {
            "kingdom": int((driver.config.get("baron_attacks") or {}).get("kingdom", 0)),
            "x": int(
                driver._selected_target_coords[0]
                if driver._selected_target_coords
                else point[0] * 1000
            ),
            "y": int(
                driver._selected_target_coords[1]
                if driver._selected_target_coords
                else point[1] * 1000
            ),
        }
        result = driver._finish_attack("nomad", fake_target, one_way, movement)
        if result in {"nomad", "client:1"} or str(result).startswith("client:"):
            sent = int((driver.store.live.session_by_mode or {}).get(self.spec_id) or 0)
            self._maybe_report_eleven(driver, sent)
            if sent >= NOMAD_SESSION_QUOTA:
                return self._finish_event(driver, sent)
            if self._camp_just_finished(driver):
                logger.info(
                    "Лагерь {} закрыт после 11 ударов — сразу беру следующий",
                    driver._selected_target_coords,
                )
                driver.advance_nomad_level_after_camp()
                driver._hunt_queue = []
                self._drop_exhausted_camps(driver)
                follow = self._attack_next_camp_now(driver)
                if follow is not None:
                    return follow
            else:
                self._keep_current_camp_queued(driver)
        return result

    def _camp_just_finished(self, driver: Any) -> bool:
        coords = driver._selected_target_coords
        if not coords:
            return False
        return not driver.store.camp_has_nomad_budget(coords)

    def _same_camp(self, target: Any, coords: tuple[int, int] | None) -> bool:
        if not coords or not getattr(target, "coords", None):
            return False
        return abs(target.coords[0] - coords[0]) <= 4 and abs(target.coords[1] - coords[1]) <= 4

    def _keep_current_camp_queued(self, driver: Any) -> None:
        coords = driver._selected_target_coords
        if not coords or not driver.store.camp_has_nomad_budget(coords):
            return
        last_point = getattr(driver, "_last_nomad_point", None)
        queue = list(getattr(driver, "_hunt_queue", []) or [])
        same = [target for target in queue if self._same_camp(target, coords)]
        others = [target for target in queue if not self._same_camp(target, coords)]
        point = last_point or (same[0].point if same else None)
        if point is None or (abs(point[0] - 0.50) < 0.02 and abs(point[1] - 0.50) < 0.02):
            farm_point = driver.store.nomad_farm_screen()
            if farm_point:
                point = farm_point
        if point is None or (abs(point[0] - 0.50) < 0.02 and abs(point[1] - 0.50) < 0.02):
            logger.info("Оставляю лагерь {} в очереди без экранной точки — сверка ±4", coords)
            point = same[0].point if same else (0.42, 0.48)
        head = HuntTarget(point, coords)
        logger.info("Оставляю лагерь {} в очереди — добиваю 11 ударов", coords)
        driver._nomad_recenter_next = True
        driver._hunt_queue = [head] + others
        driver.store.set_nomad_farm(coords, point)

    def _drop_exhausted_camps(self, driver: Any) -> None:
        coords = driver._selected_target_coords
        kept: list[Any] = []
        for target in list(getattr(driver, "_hunt_queue", []) or []):
            if self._same_camp(target, coords):
                logger.info("Убираю из очереди выбитый лагерь {}", target.coords)
                continue
            if target.coords and not driver.store.camp_has_nomad_budget(target.coords):
                logger.info("Лагерь {} уже без бюджета — следующий", target.coords)
                continue
            kept.append(target)
        driver._hunt_queue = kept
        farm = driver.store.nomad_farm_xy()
        if farm and coords and abs(farm[0] - coords[0]) <= 4 and abs(farm[1] - coords[1]) <= 4:
            driver.store.clear_nomad_farm()

    def _restore_farm_queue(self, driver: Any) -> None:
        """After restart, keep hitting the same camp instead of collecting a new yurt."""
        farm = driver.store.nomad_farm_xy()
        if farm is None:
            shot = str(driver.store.live.last_screenshot or "")
            match = re.search(r"nomad_(\d+)_(\d+)_", shot.replace("\\", "/"))
            if match:
                farm = (int(match.group(1)), int(match.group(2)))
        if farm is None:
            return
        farm = driver.store.canonicalize_nomad_coords(farm) or farm
        if not driver.store.camp_has_nomad_budget(farm):
            driver.store.clear_nomad_farm()
            return
        queue = list(getattr(driver, "_hunt_queue", []) or [])
        if queue and self._same_camp(queue[0], farm):
            return
        point = driver.store.nomad_farm_screen() or getattr(driver, "_last_nomad_point", None)
        if point is None or (abs(point[0] - 0.50) < 0.02 and abs(point[1] - 0.50) < 0.02):
            same = [t for t in list(getattr(driver, "_hunt_queue", []) or []) if self._same_camp(t, farm)]
            point = same[0].point if same else None
        if point is None:
            point = (0.42, 0.48)
        driver._last_nomad_point = point
        driver._selected_target_coords = farm
        driver._nomad_recenter_next = True
        queue = list(getattr(driver, "_hunt_queue", []) or [])
        others = [target for target in queue if not self._same_camp(target, farm)]
        driver._hunt_queue = [HuntTarget(point, farm)] + others
        driver.store.set_nomad_farm(farm, point)
        logger.info("Восстанавливаю лагерь {} — добиваю 11 ударов, чужой не беру", farm)

    def _attack_next_camp_now(self, driver: Any) -> str | None:
        """After 11 hits, open the next queued camp immediately. One follow-up per cycle."""
        if getattr(self, "_following_next_camp", False):
            return None
        ok, _ = concurrent_ok(len(driver.store.in_flight()), driver.config)
        if not ok:
            logger.info("Следующий лагерь подождёт свободного военачальника")
            return None
        self._following_next_camp = True
        try:
            if not driver._hunt_queue:
                driver._hunt_queue = driver._collect_hunt_batch("nomad")[:NOMAD_MAP_CAP]
                if driver._hunt_queue and not self._difficulty_chosen_by_bot:
                    self._probe_camp_levels(driver)
                self._drop_exhausted_camps(driver)
            if not driver._hunt_queue:
                logger.info("Других лагерей кочевников рядом нет")
                return "no_targets"
            map_shot = driver._await_world_map(timeout=8)
            if map_shot is None:
                logger.info("Карты после отправки ещё нет — следующий лагерь в новом цикле")
                return None
            target = driver._hunt_queue[0]
            if not driver.store.camp_has_nomad_budget(target.coords):
                driver._hunt_queue.pop(0)
                return None
            point = driver._focus_hunt_target("nomad", target)
            if point is None:
                logger.info("Следующий лагерь {} не на экране — пробую любой видимый", target.coords or target.point)
                driver._hunt_queue.pop(0)
                return None
            if target.coords:
                driver._selected_target_coords = target.coords
                driver.store.live.last_coords = f"K0 ({target.coords[0]}, {target.coords[1]})"
                driver.store.save()
            logger.info("Сразу открываю следующий лагерь {}", target.coords or target.point)
            if driver._open_formation(point, "nomad"):
                self._maybe_pick_difficulty_after_attack(driver)
                return self._execute(driver, point)
            driver._hunt_queue.pop(0)
            return None
        finally:
            self._following_next_camp = False

    def _maybe_pick_difficulty_after_attack(self, driver: Any) -> None:
        shot = driver._image()
        if is_difficulty_dialog(shot):
            self._pick_difficulty(driver)
            return
        if self._difficulty_checked:
            return
        self._difficulty_checked = True
        found = driver._wait_for(is_difficulty_dialog, timeout=1.2, label="сложность")
        if found is None:
            logger.info("Окна сложности нет — уже выбрана, сразу атаки")
            return
        self._pick_difficulty(driver)

    def _pick_difficulty(self, driver: Any) -> bool:
        image = driver._image()
        if not is_difficulty_dialog(image):
            return False
        point = pick_open_difficulty_point(image)
        if point is None:
            logger.info("Замочков в списке сложности нет — подтверждаю текущий выбор")
        else:
            logger.info(
                "Сложность: верхний замочек, беру открытую строку над ним ({:.3f}, {:.3f})",
                point[0],
                point[1],
            )
            driver._tap_norm_exact(*point)
            CONTROL.sleep(0.35)
        check = find_autoselect_confirm(driver._image())
        if check is None:
            fallback = (driver.layout.get("buttons") or {}).get("autoselect_confirm") or [0.77, 0.82]
            check = (float(fallback[0]), float(fallback[1]))
        logger.info("Подтверждаю сложность галочкой ({:.3f}, {:.3f})", check[0], check[1])
        driver._tap_norm_exact(*check)
        CONTROL.sleep(0.5)
        self._difficulty_chosen_by_bot = True
        self._difficulty_checked = True
        for target in list(getattr(driver, "_hunt_queue", []) or []):
            if driver.store.nomad_remaining_for(target.coords) is None:
                driver.store.set_nomad_remaining(target.coords, FRESH_CAMP_REMAINING)
        return True

    def _probe_camp_levels(self, driver: Any) -> None:
        if self._levels_probed or self._difficulty_chosen_by_bot:
            return
        self._levels_probed = True
        logger.info("Сложность уже выбрана — уровень читаю при открытии лагеря, карту не листаю")
        kept: list[Any] = []
        for target in list(driver._hunt_queue):
            coords = target.coords or (
                round(target.point[0] * 1000),
                round(target.point[1] * 1000),
            )
            if driver.store.nomad_remaining_for(coords) is None:
                driver.store.set_nomad_remaining(coords, FRESH_CAMP_REMAINING)
            kept.append(HuntTarget(target.point, coords))
            logger.info("Лагерь {} remaining={}", coords, driver.store.nomad_remaining_for(coords))
        driver._hunt_queue = kept

    def _prepare_waves(self, driver: Any) -> tuple[bool, str]:
        image = driver._image()
        if not is_formation_screen(image):
            return False, "formation_not_found"
        units = driver._read_ratio_from_image(image, "formation_units")
        tools = driver._read_ratio_from_image(image, "formation_tools")
        units_present = bool(units and units[0] > 0)
        tools_full = self._tools_full(tools)
        if self._tools_unavailable:
            logger.info("Орудия уже пустые — пресет не трогаю, сразу автоподбор")
            self._dismiss_tool_picker(driver)
        elif self._preset_ready:
            applied = self._apply_preset_only(driver)
            if not applied:
                return False, "preset_apply_failed"
            image = driver._image()
            units = driver._read_ratio_from_image(image, "formation_units")
            tools = driver._read_ratio_from_image(image, "formation_tools")
            units_present = bool(units and units[0] > 0)
            tools_full = self._tools_full(tools)
            if not tools_full:
                if bool(driver.config.get("nomad_tools_depleted")):
                    logger.info("Запас орудий с биркой исчерпан — частичный пресет, автоподбор")
                elif units_present:
                    logger.warning(
                        "Орудия не полные, в волне уже юниты — выхожу и зайду без сохранения пресета"
                    )
                    self._leave_plan(driver)
                    return False, "retry_nomad_tools"
                else:
                    filled = self._fill_support_tools(driver, refill_same=True)
                    if filled and not self._save_preset_safe(driver):
                        return False, "preset_save_blocked"
            if units_present:
                logger.info("Волны уже заполнены автоподбором — повторно диалог не открываю")
                return True, ""
            if bool(driver.config.get("nomad_waves_ready")):
                logger.info("Волны подтверждены живым автоподбором — отправляю текущий план")
                return True, ""
        else:
            filled = self._fill_support_tools(driver)
            if filled:
                image = driver._image()
                units = driver._read_ratio_from_image(image, "formation_units")
                if units and units[0] > 0:
                    logger.warning("В волне уже юниты — пресет не сохраняю, выхожу")
                    self._leave_plan(driver)
                    return False, "retry_nomad_tools"
                if not self._first_preset_flow(driver):
                    return False, "preset_setup_failed"
                self._preset_ready = True
            else:
                logger.info("Бирки в инвентаре есть, набор не встал — автоподбор, пресет не помечаю пустым")
                self._dismiss_tool_picker(driver)
        if not self._run_autoselect(driver):
            return False, "autoselect_failed"
        return True, ""

    def _tools_full(self, ratio: tuple[int, int] | None) -> bool:
        if not ratio or ratio[1] <= 0:
            return False
        return ratio[0] >= ratio[1]

    def _fill_support_tools(self, driver: Any, refill_same: bool = False) -> bool:
        """Collect badge+stock inventory, then put different types on flanks."""
        buttons = driver.layout.get("buttons") or {}
        flanks = [
            buttons.get("flank_1") or [0.22, 0.50],
            buttons.get("flank_2") or [0.38, 0.50],
            buttons.get("flank_3") or buttons.get("center_flank") or [0.54, 0.50],
        ]
        slot = buttons.get("tool_slot") or [0.62, 0.70]
        driver._tap_norm_exact(float(flanks[0][0]), float(flanks[0][1]))
        CONTROL.sleep(0.22)
        driver._tap_norm_exact(float(slot[0]), float(slot[1]))
        CONTROL.sleep(0.40)
        try:
            save_shot(driver._image(), "nomad_tools_inventory.png")
        except Exception:
            pass
        inventory = self._scan_inventory(driver)
        if not inventory:
            logger.info("Первый скан инвентаря пуст — кручу вверх и сканирую ещё раз")
            if hasattr(driver, "reset_tool_inventory_scroll"):
                driver.reset_tool_inventory_scroll(stride=INVENTORY_STRIDE)
            inventory = self._scan_inventory(driver)
        if not inventory:
            logger.info("После полного скролла бирок не видно — набор пропускаю только если инвентарь пуст")
            self._dismiss_tool_picker(driver)
            return False
        planned = list(self._planned_tools) if refill_same and self._planned_tools else assign_flank_tools(inventory, 3)
        self._planned_tools = planned
        self._dismiss_tool_picker(driver)
        used: set[str] = set()
        filled = 0
        for index, flank in enumerate(flanks, start=1):
            chosen = planned[index - 1] if index - 1 < len(planned) else None
            driver._tap_norm_exact(float(flank[0]), float(flank[1]))
            CONTROL.sleep(0.22)
            driver._tap_norm_exact(float(slot[0]), float(slot[1]))
            CONTROL.sleep(0.40)
            if chosen is None or not self._fill_chosen_tool(driver, chosen, used):
                if hasattr(driver, "reset_tool_inventory_scroll"):
                    driver.reset_tool_inventory_scroll(stride=INVENTORY_STRIDE)
                extra = [
                    item
                    for item in self._scan_inventory(driver, skip=used)
                    if item.qty > 0 or item.fingerprint
                ]
                alt = next((item for item in extra if item.fingerprint not in used), extra[0] if extra else None)
                if alt is None or not self._fill_chosen_tool(driver, alt, used):
                    logger.info("Фланг {}: нет орудия с биркой — пропускаю слот", index)
                    self._dismiss_tool_picker(driver)
                    if index == 1 and filled == 0:
                        return False
                    continue
                chosen = alt
            ratio = driver._read_ratio_from_image(driver._image(), "picker_units")
            ok, reason, _ = driver.diagnose_unit_picker_confirm(click=True, observed_fill=ratio)
            if not ok:
                logger.warning("Пикер орудий фланга {}: {}", index, reason)
                continue
            formation = driver._wait_for(is_formation_screen, timeout=6, label="план после орудий")
            if formation is None:
                continue
            used.add(chosen.fingerprint)
            filled += 1
            logger.info(
                "Фланг {}: орудие +{}% qty={} fp={}",
                index,
                chosen.percent,
                chosen.qty,
                chosen.fingerprint,
            )
        tools = driver._read_ratio_from_image(driver._image(), "formation_tools")
        logger.info("Орудия кочевников после всех фронтов: {} (флангов {})", tools, filled)
        return filled >= 1

    def _scan_inventory(
        self,
        driver: Any,
        skip: set[str] | None = None,
    ) -> list[NomadToolStock]:
        """Fast page scan: badge + stock. Empty first page is not «нет бирок»."""
        skip = skip or set()
        found: dict[str, NomadToolStock] = {}
        stagnant = 0
        for page in range(INVENTORY_PAGES):
            image = driver._image()
            page_items = find_nomad_tool_inventory(image)
            new = 0
            for item in page_items:
                if item.fingerprint in skip:
                    continue
                if item.fingerprint not in found:
                    found[item.fingerprint] = item
                    new += 1
            logger.info(
                "Инвентарь орудий шаг {}: бирок {}, новые {}",
                page + 1,
                len(found),
                new,
            )
            if new == 0 and page > 0 and found:
                stagnant += 1
                if stagnant >= 2:
                    break
            else:
                stagnant = 0
            if len(found) >= 3:
                logger.info("Инвентарь орудий: {} типов с биркой, хватает для флангов", len(found))
                break
            if page == INVENTORY_PAGES - 1:
                break
            driver.scroll_tool_inventory(stride=INVENTORY_STRIDE)
        if not found:
            logger.info("За {} шагов бирок не видно — продолжаю скролл не пропускаю набор зря", INVENTORY_PAGES)
        return list(found.values())

    def _fill_chosen_tool(
        self,
        driver: Any,
        chosen: NomadToolStock,
        used: set[str],
    ) -> bool:
        for page in range(INVENTORY_PAGES):
            items = find_nomad_tool_inventory(driver._image())
            match = [item for item in items if item.fingerprint == chosen.fingerprint]
            if not match:
                match = [
                    item
                    for item in items
                    if item.percent == chosen.percent
                    and item.fingerprint not in used
                ]
            if match:
                item = match[0]
                driver._tap_norm_exact(*item.tap)
                CONTROL.sleep(0.28)
                if not self._fill_selected_tool(driver, item.tap[1]):
                    return False
                return True
            driver.scroll_tool_inventory(stride=INVENTORY_STRIDE)
        return False

    def _fill_selected_tool(self, driver: Any, row_y: float) -> bool:
        image = driver._image()
        before = driver._read_ratio_from_image(image, "picker_units")
        max_btn = find_picker_max_control(image)
        if max_btn is not None:
            logger.info("MAX орудий ({:.3f}, {:.3f}), было {}", max_btn[0], max_btn[1], before)
            driver._tap_norm_exact(*max_btn)
            CONTROL.sleep(0.28)
            ratio = driver._read_ratio_from_image(driver._image(), "picker_units")
            if ratio and ratio[1] > 0 and ratio[0] >= max(1, ratio[1] // 2):
                logger.info("Орудия пикера после MAX {}", ratio)
                return True
            if ratio:
                before = ratio
        plus = find_tool_slider_plus(image, row_y)
        if plus is None:
            plus = (0.80, min(0.72, row_y))
        logger.info("Плюс слайдера орудий ({:.3f}, {:.3f}), было {}", plus[0], plus[1], before)
        for _ in range(20):
            driver._tap_norm_exact(*plus)
            CONTROL.sleep(0.16)
            ratio = driver._read_ratio_from_image(driver._image(), "picker_units")
            if ratio and ratio[1] > 0 and ratio[0] >= ratio[1]:
                logger.info("Орудия пикера заполнены {}", ratio)
                return True
            if ratio and before and ratio[0] > before[0]:
                before = ratio
        ratio = driver._read_ratio_from_image(driver._image(), "picker_units")
        if ratio and ratio[1] > 0 and ratio[0] > 0:
            logger.info("Орудия пикера частично {}", ratio)
            return True
        logger.info("Слайдер орудий не сдвинул OCR {} — подтверждаю выбранную бирку", ratio)
        return True

    def _dismiss_tool_picker(self, driver: Any) -> None:
        image = driver._image()
        close = find_red_cross_force(image, title_bar_only=False)
        if close and close[0] < 0.50 and close[1] > 0.55:
            driver._tap_forced(*close)
            CONTROL.sleep(0.35)
            return
        cancel = (driver.layout.get("buttons") or {}).get("picker_cancel") or [0.27, 0.789]
        logger.info("Закрываю пикер орудий красной печатью ({:.3f}, {:.3f})", float(cancel[0]), float(cancel[1]))
        driver._tap_forced(float(cancel[0]), float(cancel[1]))
        CONTROL.sleep(0.35)

    def _first_preset_flow(self, driver: Any) -> bool:
        if not self._open_presets(driver):
            return False
        if not self._tap_save_preset(driver):
            self._close_presets(driver)
            return False
        CONTROL.sleep(0.35)
        if not self._tap_apply_all(driver):
            self._close_presets(driver)
            return False
        CONTROL.sleep(0.35)
        self._tap_save_preset(driver)
        CONTROL.sleep(0.30)
        return self._close_presets(driver)

    def _apply_preset_only(self, driver: Any) -> bool:
        if not self._open_presets(driver):
            return False
        ok = self._tap_apply_all(driver)
        CONTROL.sleep(0.30)
        closed = self._close_presets(driver)
        return ok and closed

    def _save_preset_safe(self, driver: Any) -> bool:
        image = driver._image()
        units = driver._read_ratio_from_image(image, "formation_units")
        if units and units[0] > 0:
            logger.warning("Отказ: в волне есть юниты — пресет не сохраняю")
            return False
        if not self._open_presets(driver):
            return False
        saved = self._tap_save_preset(driver)
        CONTROL.sleep(0.25)
        self._close_presets(driver)
        return saved

    def _open_presets(self, driver: Any) -> bool:
        image = driver._image()
        point = find_preset_button(image)
        if point is None:
            fallback = (driver.layout.get("buttons") or {}).get("preset_menu") or [0.22, 0.93]
            point = (float(fallback[0]), float(fallback[1]))
        logger.info("Открываю предустановки ({:.3f}, {:.3f})", point[0], point[1])
        driver._tap_norm_exact(*point)
        opened = driver._wait_for(is_presets_dialog, timeout=6, label="диалог предустановок")
        return opened is not None

    def _tap_save_preset(self, driver: Any) -> bool:
        image = driver._image()
        if not is_presets_dialog(image):
            return False
        apply = find_apply_preset_all(image)
        if apply:
            point = (apply[0], max(0.40, apply[1] - 0.20))
        else:
            fallback = (driver.layout.get("buttons") or {}).get("preset_save") or [0.28, 0.64]
            point = (float(fallback[0]), float(fallback[1]))
        logger.info("Сохраняю выбранную волну как предустановку ({:.3f}, {:.3f})", point[0], point[1])
        driver._tap_norm_exact(*point)
        CONTROL.sleep(0.30)
        return True

    def _tap_apply_all(self, driver: Any) -> bool:
        image = driver._image()
        point = find_apply_preset_all(image)
        if point is None:
            fallback = (driver.layout.get("buttons") or {}).get("preset_apply_all") or [0.52, 0.84]
            point = (float(fallback[0]), float(fallback[1]))
        logger.info("Применяю предустановку ко всем волнам ({:.3f}, {:.3f})", point[0], point[1])
        driver._tap_norm_exact(*point)
        CONTROL.sleep(0.35)
        return True

    def _close_presets(self, driver: Any) -> bool:
        image = driver._image()
        if not is_presets_dialog(image):
            return is_formation_screen(image) or find_preset_button(image) is not None
        point = find_preset_dialog_close(image)
        if point is None:
            fallback = (driver.layout.get("buttons") or {}).get("preset_close") or [0.88, 0.16]
            point = (float(fallback[0]), float(fallback[1]))
        logger.info("Закрываю диалог предустановок крестиком ({:.3f}, {:.3f}) — не план атаки", point[0], point[1])
        driver._tap_forced(*point)
        CONTROL.sleep(0.40)
        latest = driver._image()
        return is_formation_screen(latest) and not is_presets_dialog(latest)

    def _run_autoselect(self, driver: Any) -> bool:
        self._dismiss_tool_picker(driver)
        CONTROL.sleep(0.30)
        self._dismiss_tool_picker(driver)
        image = driver._wait_for(is_formation_screen, timeout=8, label="план перед автоподбором")
        if image is None:
            image = driver._image()
        if not is_formation_screen(image):
            if find_autoselect_button(image) is None and find_formation_attack_button(image) is None:
                logger.warning("Плана атаки нет — автоподбор не жму")
                return False
            logger.info("План по кнопкам автоподбора/нападения — продолжаю")
        point = find_autoselect_button(image)
        if point is None:
            known = (driver.layout.get("buttons") or {}).get("autoselect") or [0.069, 0.963]
            point = (float(known[0]), float(known[1]))
            logger.info("Шаблон автоподбора слаб — жму точку плана ({:.3f}, {:.3f})", point[0], point[1])
        else:
            point = (point[0], min(point[1], 0.945))
            logger.info("Автоподбор волн ({:.3f}, {:.3f})", point[0], point[1])
        dialog = None
        for attempt in range(3):
            driver._tap_norm_exact(*point)
            dialog = driver._wait_for(
                is_autoselect_dialog,
                timeout=2.5,
                label=f"автоподбор волн {attempt + 1}/3",
            )
            if dialog is not None:
                break
            CONTROL.sleep(0.30)
        if dialog is None:
            return False
        check = find_autoselect_confirm(dialog)
        if check is None:
            fallback = (driver.layout.get("buttons") or {}).get("autoselect_confirm") or [0.77, 0.82]
            check = (float(fallback[0]), float(fallback[1]))
        logger.info("Галочка автоподбора справа снизу ({:.3f}, {:.3f})", check[0], check[1])
        back = None
        for attempt in range(3):
            driver._tap_norm_exact(*check)
            CONTROL.sleep(0.45)
            back = driver._wait_for(
                is_formation_screen,
                timeout=2.5,
                label=f"план после автоподбора {attempt + 1}/3",
            )
            if back is not None:
                break
        if back is None:
            return False
        attack = find_formation_attack_button(back)
        if attack is None:
            logger.warning("Кнопка Нападение после автоподбора не найдена")
        return True

    def _leave_plan(self, driver: Any) -> None:
        logger.warning("Выхожу из плана кочевников, чтобы не сохранить юнитов в пресет")
        driver.close_formation_plan()

    def _harvest_battle_reports(self, driver: Any) -> dict[str, int]:
        """Open bottom-nav Messages, read every battle report, sum all resources."""
        totals: dict[str, int] = {}
        seen: set[str] = set()
        driver._dismiss_reward_popups()
        image = driver._image()
        if not is_inbox_screen(image):
            nav = find_messages_nav(image)
            if nav is None:
                fallback = (driver.layout.get("buttons") or {}).get("messages") or [0.69, 0.94]
                nav = (float(fallback[0]), float(fallback[1]))
            logger.info("Открываю сообщения нижней панели ({:.3f}, {:.3f})", nav[0], nav[1])
            driver._tap_norm_exact(*nav)
            CONTROL.sleep(0.8)
            opened = driver._wait_for(is_inbox_screen, timeout=6, label="входящие")
            if opened is None:
                logger.warning("Входящие не открылись — отчёт по письмам пропускаю")
                return totals
        for row in range(12):
            shot = driver._image()
            if not is_inbox_screen(shot):
                break
            y = 0.22 + row * 0.055
            if y > 0.78:
                break
            driver._tap_norm_exact(0.50, y)
            CONTROL.sleep(0.55)
            report = driver._image()
            if is_inbox_screen(report):
                continue
            blob = ocr_text_ui(crop_rel(report, [0.08, 0.10, 0.92, 0.88]), psm=6)
            compact = blob.lower().replace("ё", "е")
            relevant = any(
                token in compact
                for token in ("кочевн", "nomad", "атак", "отчет", "отчёт", "добыч", "наград")
            )
            if relevant:
                parsed = parse_report_resources(blob)
                digest = re.sub(r"\s+", " ", compact)[:180]
                if digest in seen:
                    logger.info("Письмо уже учтено — пропускаю повтор")
                else:
                    seen.add(digest)
                    logger.info("Отчёт кочевников: {} / {}", parsed, blob[:120])
                    for key, value in parsed.items():
                        totals[key] = int(totals.get(key) or 0) + int(value)
                    try:
                        save_shot(report, f"nomad_report_{row}.png")
                    except Exception:
                        pass
            close = find_red_cross_force(report, title_bar_only=True) or find_red_cross_force(report)
            if close and close[1] < 0.18:
                driver._tap_forced(*close)
            else:
                driver.adb.key(4)
            CONTROL.sleep(0.45)
        driver._dismiss_inbox_if_open()
        return totals

    def _finish_event(self, driver: Any, sent: int) -> str:
        loot = self._harvest_battle_reports(driver)
        gold = int(loot.get("gold") or 0)
        rubies = int(loot.get("rubies") or 0)
        if gold or rubies:
            driver.store.record_loot(gold=gold, rubies=rubies)
        summary = driver.store.session_summary()
        logger.info(
            "Вторжение кочевников закрыто: {} атак, добыча {}",
            sent,
            loot,
        )
        driver.telegram.report_nomad_complete(
            attacks=sent,
            gold=int(summary["gold"] or gold),
            rubies=int(summary["rubies"] or rubies),
            resources=loot,
        )
        self._persist_final_report(sent, loot, summary)
        driver.store.skip_mode(self.spec_id)
        return "nomad_complete"

    def _persist_final_report(self, sent: int, loot: dict[str, int], summary: dict[str, int]) -> None:
        from e4kbot.paths import DATA_DIR

        payload = {
            "mode": self.spec_id,
            "attacks": int(sent),
            "resources": dict(loot),
            "session": dict(summary),
        }
        path = DATA_DIR / "nomad_final_report.json"
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        logger.info("Итоговый отчёт кочевников сохранён: {}", path)

    def _maybe_report_eleven(self, driver: Any, sent: int) -> None:
        coords = driver._selected_target_coords
        hits = driver.store.target_hits("nomad", coords) if coords else 0
        if hits < NOMAD_HITS_PER_CAMP or self._eleven_reported:
            return
        self._eleven_reported = True
        remaining = {
            str(key): int(value)
            for key, value in (driver.store.live.nomad_remaining or {}).items()
        }
        payload = {
            "mode": self.spec_id,
            "attacks": int(sent),
            "last_camp": list(coords) if coords else None,
            "hits_on_last_camp": int(hits),
            "remaining_by_camp": remaining,
        }
        from e4kbot.paths import DATA_DIR

        path = DATA_DIR / "nomad_eleven_report.json"
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        text = (
            "✅ Кочевники: 11 атак отправлено\n"
            f"🎯 Последний лагерь: {coords}\n"
            f"⚔️ Ударов по нему: {hits}\n"
            "После 11 ударов по одному лагерю сразу беру следующий."
        )
        logger.info("Отчёт 11 атак кочевников: {}", text.replace("\n", " | "))
        sent_ok = driver.telegram.send_text(text, kind="nomad")
        if not sent_ok:
            logger.warning("Telegram выключен или без токена — отчёт сохранён в {}", path)


def wave_has_units(ratio: tuple[int, int] | None) -> bool:
    return bool(ratio and ratio[0] > 0)
