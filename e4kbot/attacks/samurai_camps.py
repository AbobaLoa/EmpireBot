from __future__ import annotations

import re
from typing import Any

from loguru import logger

from e4kbot.bluestacks import save_shot
from e4kbot.control import CONTROL
from e4kbot.client import HuntTarget, samurai_coords_allowed
from e4kbot.vision import (
    FEATHER_HORSE_MIN_X,
    FEATHER_HORSE_POINT,
    crop_rel,
    find_apply_preset_all,
    find_autoselect_button,
    find_autoselect_confirm,
    find_dialog_green_check,
    find_feather_horse,
    find_formation_attack_button,
    find_formation_tool_slots,
    find_in_stock_tool_plus,
    find_picker_confirm_button,
    find_preset_button,
    find_preset_dialog_close,
    find_red_cross_force,
    find_reward_confirm,
    find_save_preset_button,
    find_target_attack_button,
    find_tool_row_minus,
    find_start_attack_gate_seals,
    find_travel_seal_pair,
    is_autoselect_dialog,
    is_difficulty_dialog,
    is_event_reward_popup,
    is_feather_selected,
    is_formation_screen,
    is_map_screen,
    is_no_commanders_parchment,
    is_presets_dialog,
    is_ruby_horse_selected,
    is_save_preset_dialog,
    is_special_offers_screen,
    is_start_attack_gate,
    is_tool_catalog,
    is_travel_dialog,
    ocr_text,
    ocr_text_ui,
    parse_count,
    parse_ratio,
    parse_samurai_camp_level,
    pick_open_difficulty_point,
    read_tool_row_ratio,
    remaining_attacks_from_level,
)

SAMURAI_CAMP_LIMIT = 10
SAMURAI_MAP_CAP = 4
SAMURAI_SESSION_QUOTA = 44
FRESH_CAMP_REMAINING = 10


def merge_tool_bonus_pages(
    pages: list[list[tuple[int, float, float]]],
) -> list[tuple[int, float, float]]:
    """Keep one tap point per +% value; lowest percent first."""
    best: dict[int, tuple[int, float, float]] = {}
    for page in pages:
        for item in page:
            prev = best.get(item[0])
            if prev is None:
                best[item[0]] = item
    return sorted(best.values(), key=lambda row: row[0])


class SamuraiCampsModule:
    """Samurai Invasion: tools + presets + autoselect, 11 hits per camp, 4 camps."""

    spec_id = "samurai_camps"
    is_stub = False

    def __init__(self) -> None:
        self._preset_ready = False
        self._difficulty_chosen_by_bot = False
        self._difficulty_checked = False
        self._levels_probed = False
        self._chosen_tool_percent: int | None = None
        self._tools_unavailable = False

    def run_cycle(self, driver: Any | None = None) -> str:
        if driver is None:
            return "idle"
        if bool(driver.config.get("samurai_preset_ready")):
            self._preset_ready = True
            self._chosen_tool_percent = 3
        if driver.wait_out_loading():
            return "map_loading"
        sent = int((driver.store.live.session_by_mode or {}).get(self.spec_id) or 0)
        if sent >= SAMURAI_SESSION_QUOTA:
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
        plain_map = is_map_screen(current) and find_reward_confirm(current) is None
        if not plain_map:
            driver._dismiss_connection_error_if_open(current)
            driver._dismiss_reward_popups()
            driver._dismiss_special_offers_if_open(current)
            driver._dismiss_hire_menu_if_open()
            driver._dismiss_inbox_if_open()
            driver._dismiss_blocking_menu_if_no_camps()
            driver._dismiss_taxes_if_open()
        current = driver._image()
        resumed = self._resume_open_plan(driver, current)
        if resumed is not None:
            return resumed
        if not driver._hunt_queue:
            driver._blocked_screen_targets.clear()
            driver._hunt_queue = driver._collect_hunt_batch("samurai")[:SAMURAI_MAP_CAP]
            if not driver._hunt_queue:
                logger.warning("Нашествие самураев не на карте — пропускаю, следующий включённый приоритет")
                driver.store.skip_mode(self.spec_id)
                return "no_targets"
            logger.info("Самураи: {} лагерей рядом, дальше по списку", len(driver._hunt_queue))
            if not self._difficulty_chosen_by_bot:
                self._probe_camp_levels(driver)
        while driver._hunt_queue:
            current = driver._image()
            if is_difficulty_dialog(current):
                self._pick_difficulty(driver)
                current = driver._image()
            plain_map = is_map_screen(current) and find_reward_confirm(current) is None
            if not plain_map:
                driver._dismiss_reward_popups()
                driver._dismiss_special_offers_if_open(current)
                driver._dismiss_hire_menu_if_open()
                driver._dismiss_inbox_if_open()
                driver._dismiss_taxes_if_open()
            current = driver._image()
            resumed = self._resume_open_plan(driver, current)
            if resumed is not None:
                return resumed
            target = driver._hunt_queue[0]
            if not driver.store.camp_has_samurai_budget(target.coords):
                logger.info(
                    "Лагерь {} без оставшихся атак — следующий",
                    target.coords or target.point,
                )
                driver._hunt_queue.pop(0)
                continue
            point = driver._focus_hunt_target("samurai", target)
            if point is None:
                logger.info(
                    "Лагерь {} не совпал — чужой шатёр и замок игрока не трогаю",
                    target.coords or target.point,
                )
                driver._hunt_queue.pop(0)
                continue
            if target.coords:
                driver._selected_target_coords = target.coords
                driver.store.live.last_coords = f"K0 ({target.coords[0]}, {target.coords[1]})"
                driver.store.save()
            if driver._open_formation(point, "samurai"):
                self._maybe_pick_difficulty_after_attack(driver)
                return self._execute(driver, point)
            if getattr(driver, "_no_commanders_seen", False):
                return "no_commanders"
            driver._hunt_queue.pop(0)
        return "no_targets"

    def _resume_open_plan(self, driver: Any, current: Any) -> str | None:
        """Continue only on the real planning screen. Stray tool picker is closed."""
        if is_event_reward_popup(current):
            driver._dismiss_reward_popups(current)
            return "popup_dismissed"
        if is_special_offers_screen(current):
            driver._dismiss_special_offers_if_open(current)
            return "popup_dismissed"
        if is_start_attack_gate(current):
            coords = getattr(driver, "_selected_target_coords", None)
            if not samurai_coords_allowed(coords):
                logger.error("«Начать нападение» при цели {} — не шатёр, красная печать", coords)
                self._cancel_gate_or_travel(driver, current)
                return "player_target_aborted"
            logger.info("«Начать нападение» — галочка в план, не отправка")
            entered = self._enter_plan_from_gate(driver)
            if entered:
                return self._execute(driver, (0.50, 0.50))
            return "start_attack_gate_failed"
        if is_formation_screen(current):
            if not driver._selected_target_coords:
                match = re.search(
                    r"\((\d+)\s*,\s*(\d+)\)",
                    str(driver.store.live.last_coords or ""),
                )
                if match:
                    driver._selected_target_coords = (
                        int(match.group(1)),
                        int(match.group(2)),
                    )
            logger.info("План самураев уже открыт — продолжаю орудия/предустановки")
            if not samurai_coords_allowed(driver._selected_target_coords):
                logger.error(
                    "План открыт, координаты {} не из списка шатров — закрываю, замок не бью",
                    driver._selected_target_coords,
                )
                driver.close_formation_plan()
                return "player_target_aborted"
            return self._execute(driver, (0.50, 0.50))
        if is_travel_dialog(current):
            pair = find_travel_seal_pair(current)
            if pair is not None:
                _green, red = pair
                logger.warning("Поход без орудий — красная печать ({:.3f}, {:.3f})", red[0], red[1])
                driver._tap_norm_exact(*red)
            else:
                cancel = (driver.layout.get("buttons") or {}).get("travel_cancel") or [0.23, 0.815]
                driver._tap_norm_exact(float(cancel[0]), float(cancel[1]))
            CONTROL.sleep(0.45)
            return "travel_without_tools"
        if driver._plan_or_picker_open(current) and not is_map_screen(current):
            logger.info("Пикер без плана атаки — закрываю крестиком, не магазин")
            self._dismiss_tool_picker(driver)
            latest = driver._image()
            if is_formation_screen(latest):
                return self._execute(driver, (0.50, 0.50))
        return None

    def _execute(self, driver: Any, point: tuple[float, float]) -> str:
        if not samurai_coords_allowed(getattr(driver, "_selected_target_coords", None)):
            logger.error(
                "Цель {} не шатёр из списка — Нападение не жму",
                getattr(driver, "_selected_target_coords", None),
            )
            current = driver._image()
            if is_travel_dialog(current):
                self._cancel_travel(driver)
            elif is_formation_screen(current) or is_start_attack_gate(current):
                if is_start_attack_gate(current):
                    self._cancel_gate_or_travel(driver, current)
                else:
                    driver.close_formation_plan()
            return "player_target_aborted"
        if driver._dismiss_no_commanders():
            return "no_commanders"
        current = driver._image()
        if is_formation_screen(current):
            pass
        elif is_start_attack_gate(current):
            logger.info("Вход в план через «Начать нападение»")
            if not self._enter_plan_from_gate(driver):
                return "start_attack_gate_failed"
            current = driver._image()
        elif is_travel_dialog(current):
            pair = find_travel_seal_pair(current)
            if pair is not None:
                _green, red = pair
                logger.warning("Поход открыт до орудий — красная печать ({:.3f}, {:.3f})", red[0], red[1])
                driver._tap_norm_exact(*red)
            else:
                cancel = (driver.layout.get("buttons") or {}).get("travel_cancel") or [0.23, 0.815]
                driver._tap_norm_exact(float(cancel[0]), float(cancel[1]))
            CONTROL.sleep(0.45)
            current = driver._image()
        self._maybe_pick_difficulty_after_attack(driver)
        if not is_formation_screen(driver._image()):
            formation = driver._wait_for(is_formation_screen, timeout=10, label="план самураев")
            if formation is None:
                logger.warning("Нет плана атаки самураев")
                return "formation_not_found"
        prepared, reason = self._prepare_waves(driver)
        if not prepared:
            driver.store.live.last_error = reason
            driver.store.save()
            logger.warning("Самураи: подготовка не прошла ({}) — Нападение не жму", reason)
            return reason
        driver._tap_formation_attack()
        travel = driver._wait_for(
            lambda img: is_travel_dialog(img) or is_no_commanders_parchment(img),
            timeout=driver._vision_seconds("travel_dialog_timeout_seconds", 15),
            label="диалог похода самураев",
        )
        if travel is not None and driver._dismiss_no_commanders(travel):
            return "no_commanders"
        if travel is None:
            if driver._dismiss_no_commanders():
                return "no_commanders"
            logger.warning("Нет диалога похода самураев")
            return "travel_dialog_not_found"
        travel = driver._image()
        movement = self._pick_feather(driver, travel)
        if movement != "feather":
            logger.error("Перо не выбрано ({}) — поход не подтверждаю", movement)
            return movement
        travel = driver._image()
        if is_ruby_horse_selected(travel) or not is_feather_selected(travel):
            logger.error("Перед галочкой всё ещё рубиновый конь — отмена")
            self._cancel_travel(driver)
            return "ruby_movement_refused"
        one_way = driver._read_march_time(travel)
        if one_way is None:
            one_way = 30
            logger.info("Время похода не прочиталось — беру 30с для ближнего лагеря")
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
        result = driver._finish_attack("samurai", fake_target, one_way, movement)
        if result in {"samurai", "client:1"} or str(result).startswith("client:"):
            sent = int((driver.store.live.session_by_mode or {}).get(self.spec_id) or 0)
            if sent >= SAMURAI_SESSION_QUOTA:
                return self._finish_event(driver, sent)
        return result

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
            if driver.store.samurai_remaining_for(target.coords) is None:
                driver.store.set_samurai_remaining(target.coords, FRESH_CAMP_REMAINING)
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
            if driver.store.samurai_remaining_for(coords) is None:
                driver.store.set_samurai_remaining(coords, FRESH_CAMP_REMAINING)
            kept.append(HuntTarget(target.point, coords))
            logger.info("Лагерь {} remaining={}", coords, driver.store.samurai_remaining_for(coords))
        driver._hunt_queue = kept

    def _read_camp_remaining(self, driver: Any, target: Any) -> tuple[int | None, tuple[int, int] | None]:
        point = driver._focus_hunt_target("samurai", target) or target.point
        driver._tap_norm_exact(*point)
        popup = driver._wait_for(
            lambda img: find_target_attack_button(img) is not None,
            timeout=4,
            label="табличка лагеря",
        )
        if popup is None:
            self._close_camp_popup(driver)
            return None, target.coords
        x_region = (driver.layout.get("regions") or {}).get("viewport_x")
        y_region = (driver.layout.get("regions") or {}).get("viewport_y")
        target_x = parse_count(ocr_text(crop_rel(popup, x_region), psm=6)) if x_region else None
        target_y = parse_count(ocr_text(crop_rel(popup, y_region), psm=6)) if y_region else None
        coords = (target_x, target_y) if target_x is not None and target_y is not None else target.coords
        if coords is not None:
            driver._selected_target_coords = coords
        title = ocr_text_ui(crop_rel(popup, [0.18, 0.14, 0.82, 0.32]), psm=6)
        body = ocr_text_ui(crop_rel(popup, [0.18, 0.20, 0.82, 0.55]), psm=6)
        blob = f"{title} {body}"
        level = parse_samurai_camp_level(blob)
        logger.info("OCR уровня лагеря: {} / {}", level, blob[:80])
        try:
            save_shot(popup, f"samurai_level_{coords[0] if coords else 0}_{coords[1] if coords else 0}.png")
        except Exception:
            pass
        self._close_camp_popup(driver)
        if level is None:
            return None, coords
        return remaining_attacks_from_level(level), coords

    def _close_camp_popup(self, driver: Any) -> None:
        image = driver._image()
        if is_map_screen(image) and find_target_attack_button(image) is None:
            return
        close = find_red_cross_force(image, title_bar_only=False)
        if close and close[0] < 0.82:
            driver._tap_norm_exact(*close)
            CONTROL.sleep(0.4)
            return
        driver.tap_rel("map")
        CONTROL.sleep(0.45)

    def _cancel_travel(self, driver: Any) -> None:
        image = driver._image()
        pair = find_travel_seal_pair(image)
        if pair is not None:
            _green, red = pair
            logger.warning("Отмена похода красной печатью ({:.3f}, {:.3f})", red[0], red[1])
            driver._tap_norm_exact(*red)
        else:
            cancel = (driver.layout.get("buttons") or {}).get("travel_cancel") or [0.23, 0.815]
            logger.warning("Отмена похода ({:.3f}, {:.3f})", float(cancel[0]), float(cancel[1]))
            driver._tap_norm_exact(float(cancel[0]), float(cancel[1]))
        CONTROL.sleep(0.45)

    def _pick_feather(self, driver: Any, travel: Any) -> str:
        """Rightmost winged horse (1 feather). Never confirm a 21-ruby warhorse."""
        point = find_feather_horse(travel) or FEATHER_HORSE_POINT
        if point[0] < FEATHER_HORSE_MIN_X:
            logger.warning(
                "find_feather_horse слишком лево ({:.3f}, {:.3f}) — беру правую карту {}",
                point[0],
                point[1],
                FEATHER_HORSE_POINT,
            )
            point = FEATHER_HORSE_POINT
        logger.info("Атака с пером, правая карта ({:.3f}, {:.3f})", point[0], point[1])
        for attempt in range(3):
            driver._tap_norm_exact(*point)
            CONTROL.sleep(0.45)
            image = driver._image()
            if is_ruby_horse_selected(image):
                logger.warning(
                    "Выбран рубиновый +41% конь, попытка {} — снова жму правую карту пера",
                    attempt + 1,
                )
                point = FEATHER_HORSE_POINT
                continue
            if is_feather_selected(image):
                logger.info("Перо выбрано, рубинов нет")
                try:
                    save_shot(image, "samurai_movement_feather.png")
                except Exception:
                    pass
                return "feather"
            logger.info("Перо ещё не подсвечено, попытка {}", attempt + 1)
        image = driver._image()
        try:
            save_shot(image, "samurai_movement_ruby_refused.png")
        except Exception:
            pass
        logger.error("Рубиновый конь / перо не подтвердилось — отмена похода")
        self._cancel_travel(driver)
        return "ruby_movement_refused"

    def _cancel_gate_or_travel(self, driver: Any, image: Any | None = None) -> bool:
        """Red seal on compact «Начать нападение» or the bottom travel dialog."""
        current = image if image is not None else driver._image()
        pair = find_start_attack_gate_seals(current) or find_travel_seal_pair(current)
        if pair is None:
            return False
        _green, red = pair
        logger.warning("Красная печать гейта/похода ({:.3f}, {:.3f})", red[0], red[1])
        driver._tap_norm_exact(*red)
        CONTROL.sleep(0.4)
        return True

    def _enter_plan_from_gate(self, driver: Any) -> bool:
        image = driver._image()
        pair = find_start_attack_gate_seals(image)
        if pair is None:
            return False
        green, _red = pair
        logger.info("Галочка «Начать нападение» в план ({:.3f}, {:.3f})", green[0], green[1])
        driver._tap_norm_exact(*green)
        opened = driver._wait_for(is_formation_screen, timeout=8, label="план после печати")
        return opened is not None

    def _prepare_waves(self, driver: Any) -> tuple[bool, str]:
        image = driver._image()
        if not is_formation_screen(image):
            return False, "formation_not_found"
        units = driver._read_ratio_from_image(image, "formation_units")
        units_present = bool(units and units[0] > 0)
        tools_ready = self._tools_ready(driver, image)
        if units_present and not tools_ready:
            logger.info("В волне уже юниты, орудия пустые — чищу волну, иначе пресет испортится")
            self._clear_selected_wave(driver)
            image = driver._image()
            units = driver._read_ratio_from_image(image, "formation_units")
            units_present = bool(units and units[0] > 0)
        if self._preset_ready:
            applied = self._apply_preset_only(driver)
            if not applied:
                return False, "preset_apply_failed"
            self._select_front_flank(driver)
            if not self._tools_ready(driver):
                logger.info("После пресета орудия не максимум — выбираю другое и сохраняю снова")
                filled = self._fill_support_tools(driver)
                if not filled:
                    return False, "tools_not_filled"
                if not self._save_preset_safe(driver):
                    return False, "preset_save_blocked"
            image = driver._image()
            units = driver._read_ratio_from_image(image, "formation_units")
            if units and units[0] > 0:
                logger.info("Волны уже с солдатами — автоподбор не повторяю")
                return True, ""
        else:
            filled = self._fill_support_tools(driver)
            if not filled:
                logger.warning("Орудия не выставлены — Нападение не жму")
                return False, "tools_not_filled"
            image = driver._image()
            units = driver._read_ratio_from_image(image, "formation_units")
            if units and units[0] > 0:
                logger.warning("В волне уже юниты — пресет не сохраняю, чищу и заново")
                self._clear_selected_wave(driver)
                filled = self._fill_support_tools(driver)
                if not filled:
                    return False, "tools_not_filled"
            if not self._first_preset_flow(driver):
                return False, "preset_setup_failed"
            self._preset_ready = True
        if not self._run_autoselect(driver):
            return False, "autoselect_failed"
        tools = self._read_tools_ratio(driver)
        if tools and tools[1] >= 10 and not self._tools_full(tools):
            logger.warning("После автоподбора орудия не полные {} — Нападение не жму", tools)
            return False, "tools_not_filled"
        return True, ""

    def _tools_full(self, ratio: tuple[int, int] | None) -> bool:
        if not ratio or ratio[1] < 10:
            return False
        return ratio[0] >= ratio[1]

    def _read_tools_ratio(self, driver: Any, image: Any | None = None) -> tuple[int, int] | None:
        image = image if image is not None else driver._image()
        tools = driver._read_ratio_from_image(image, "formation_tools")
        if tools and tools[1] >= 10:
            return tools
        wider = parse_ratio(ocr_text(crop_rel(image, [0.42, 0.58, 0.84, 0.74]), psm=6))
        if wider and wider[1] >= 10:
            logger.info("Орудия OCR расширенный регион {}", wider)
            return wider
        return tools

    def _tools_ready(self, driver: Any, image: Any | None = None) -> bool:
        """True when tools are maxed. Junk OCR like 0/4 does not overwrite a filled 40/40 slot."""
        image = image if image is not None else driver._image()
        tools = self._read_tools_ratio(driver, image)
        if self._tools_full(tools):
            return True
        if tools is not None and tools[1] >= 10:
            return False
        empty = find_formation_tool_slots(image)
        if 0 < len(empty) < 3:
            logger.info(
                "Орудия: OCR {} пустых + {} — слот уже занят, не перезаписываю",
                tools,
                len(empty),
            )
            return True
        return False

    def _flank_points(self, driver: Any) -> list[tuple[str, list[float]]]:
        buttons = driver.layout.get("buttons") or {}
        return [
            ("фронт", list(buttons.get("flank_2") or buttons.get("center_flank") or [0.138, 0.51])),
            ("левый", list(buttons.get("flank_1") or [0.052, 0.51])),
            ("правый", list(buttons.get("flank_3") or [0.218, 0.51])),
        ]

    def _select_front_flank(self, driver: Any) -> None:
        _name, front = self._flank_points(driver)[0]
        driver._tap_norm_exact(float(front[0]), float(front[1]))
        CONTROL.sleep(0.35)

    def _ensure_formation(self, driver: Any) -> bool:
        image = driver._image()
        if (
            is_formation_screen(image)
            and find_picker_confirm_button(image) is None
            and not is_tool_catalog(image)
        ):
            return True
        self._dismiss_tool_picker(driver)
        back = driver._wait_for(is_formation_screen, timeout=5, label="план перед флангом")
        return back is not None

    def _clear_selected_wave(self, driver: Any) -> None:
        point = (driver.layout.get("buttons") or {}).get("wave_clear") or [0.87, 0.595]
        logger.info("Очищаю выбранную волну ({:.3f}, {:.3f})", float(point[0]), float(point[1]))
        driver._tap_norm_exact(float(point[0]), float(point[1]))
        CONTROL.sleep(0.35)

    def _fill_support_tools(self, driver: Any) -> bool:
        """Place the same in-stock tool on FRONT, then LEFT, then RIGHT. Never courtyard."""
        filled = 0
        for name, flank in self._flank_points(driver):
            if not self._ensure_formation(driver):
                logger.warning("Нет плана перед флангом {} — три фланга не заполняю", name)
                return False
            logger.info("Переключаю фланг {} ({:.3f}, {:.3f})", name, float(flank[0]), float(flank[1]))
            driver._tap_norm_exact(float(flank[0]), float(flank[1]))
            CONTROL.sleep(0.45)
            if not self._fill_flank_tools(driver, name):
                logger.warning("Фланг {} без орудий", name)
                if not self._ensure_formation(driver):
                    logger.warning("Пикер не закрылся после фланга {} — стоп", name)
                    return False
                continue
            filled += 1
            logger.info("Фланг {} готов ({}/3)", name, filled)
        self._select_front_flank(driver)
        tools = self._read_tools_ratio(driver)
        logger.info("Орудия после фронт/левый/правый: {} (флангов {})", tools, filled)
        if filled < 3:
            logger.warning("Орудия не на всех трёх флангах ({}/3) — пресет не сохраняю", filled)
            return False
        return True

    def _fill_flank_tools(self, driver: Any, name: str) -> bool:
        """Always open a slot on this flank. Do not skip because the previous flank was 40/40."""
        placed = False
        for slot_index in range(3):
            slot = self._empty_tool_slot(driver)
            logger.info("Фланг {} слот {}: плюс орудий ({:.3f}, {:.3f})", name, slot_index + 1, slot[0], slot[1])
            driver._tap_norm_exact(*slot)
            CONTROL.sleep(0.55)
            try:
                save_shot(driver._image(), f"samurai_tools_{name}_{slot_index + 1}.png")
            except Exception:
                pass
            image = driver._image()
            picker_closed = (
                is_formation_screen(image)
                and find_picker_confirm_button(image) is None
                and not is_tool_catalog(image)
            )
            if picker_closed:
                tools = self._read_tools_ratio(driver, image)
                if self._tools_full(tools) or placed:
                    logger.info("Фланг {}: пикер не открылся, орудия уже {}", name, tools)
                    return True
            plus = self._first_in_stock_plus(driver)
            if plus is None:
                logger.info("Фланг {}: на экране нет орудий в наличии — скролл", name)
                if hasattr(driver, "scroll_tool_inventory"):
                    driver.scroll_tool_inventory()
                    CONTROL.sleep(0.35)
                    plus = self._first_in_stock_plus(driver)
            if plus is None:
                if placed:
                    self._confirm_tool_picker(driver)
                    return True
                self._dismiss_tool_picker(driver)
                return False
            row = read_tool_row_ratio(driver._image(), plus)
            if row and row[1] >= 10 and row[0] >= row[1]:
                logger.info("Фланг {}: ряд уже полный {}", name, row)
            elif not self._fill_selected_tool(driver, plus):
                logger.warning("Фланг {}: не заполнил 0/40 без магазина", name)
                self._dismiss_tool_picker(driver)
                return placed
            confirmed = False
            for _ in range(3):
                if self._confirm_tool_picker(driver):
                    confirmed = True
                    break
                CONTROL.sleep(0.25)
            if not confirmed:
                logger.warning("Фланг {}: галочка пикера не вернула план", name)
                return False
            formation = driver._wait_for(is_formation_screen, timeout=6, label="план после орудий")
            if formation is None:
                logger.warning("Фланг {}: нет плана после орудий — фланг не считаю", name)
                return False
            placed = True
            logger.info("Фланг {}: слот орудий выставлен, дальше следующий фланг", name)
            return True
        return placed

    def _empty_tool_slot(self, driver: Any) -> tuple[float, float]:
        slots = find_formation_tool_slots(driver._image())
        if slots:
            return slots[0]
        known = (driver.layout.get("buttons") or {}).get("tool_slot") or [0.62, 0.70]
        return (float(known[0]), float(known[1]))

    def _first_in_stock_plus(self, driver: Any) -> tuple[float, float] | None:
        image = driver._image()
        plus = find_in_stock_tool_plus(image)
        if plus is not None:
            ratio = read_tool_row_ratio(image, plus)
            logger.info("Первое орудие в наличии plus=({:.3f},{:.3f}) ряд {}", plus[0], plus[1], ratio)
        return plus

    def _fill_selected_tool(self, driver: Any, plus: tuple[float, float]) -> bool:
        """Minus once like soldiers = full 0/40. Plus only if minus did not fill. Never ruby cart."""
        image = driver._image()
        minus = find_tool_row_minus(image, plus)
        before = read_tool_row_ratio(image, plus)
        if before and before[1] >= 10 and before[0] >= before[1]:
            logger.info("Орудия уже максимум {}", before)
            return True
        if not (before and before[0] > 0):
            logger.info("Минус полной волны орудий ({:.3f}, {:.3f}), было {}", minus[0], minus[1], before)
            driver._tap_norm_exact(*minus)
            CONTROL.sleep(0.35)
            ratio = read_tool_row_ratio(driver._image(), plus)
            if ratio and ratio[1] >= 10 and ratio[0] >= ratio[1]:
                logger.info("Орудия после минуса {}", ratio)
                return True
            logger.info("Минус не заполнил {} — жму плюс ряда до максимума", ratio)
        else:
            ratio = before
            logger.info("В ряду уже {} — минус не жму, добираю плюсом", ratio)
        for _ in range(45):
            driver._tap_norm_exact(*plus)
            CONTROL.sleep(0.10)
            ratio = read_tool_row_ratio(driver._image(), plus)
            if ratio and ratio[1] >= 10 and ratio[0] >= ratio[1]:
                logger.info("Орудия пикера заполнены {}", ratio)
                return True
        ratio = read_tool_row_ratio(driver._image(), plus)
        return bool(ratio and ratio[1] >= 10 and ratio[0] > 0)

    def _confirm_tool_picker(self, driver: Any) -> bool:
        image = driver._image()
        if is_formation_screen(image) and find_picker_confirm_button(image) is None:
            logger.info("Пикер орудий уже закрыт — галочку не жму")
            return True
        check = find_picker_confirm_button(image) or find_dialog_green_check(image)
        if check is None:
            fallback = (driver.layout.get("buttons") or {}).get("picker_confirm") or [0.727, 0.789]
            check = (float(fallback[0]), float(fallback[1]))
        if is_formation_screen(image) and check[1] > 0.90:
            logger.info("Галочка пикера совпала бы с Нападением — не жму")
            return True
        logger.info("Галочка пикера орудий ({:.3f}, {:.3f})", check[0], check[1])
        driver._tap_norm_exact(*check)
        CONTROL.sleep(0.45)
        back = driver._wait_for(is_formation_screen, timeout=5, label="план после галочки орудий")
        return back is not None

    def _dismiss_tool_picker(self, driver: Any) -> None:
        image = driver._image()
        if (
            is_formation_screen(image)
            and find_picker_confirm_button(image) is None
            and not is_tool_catalog(image)
        ):
            return
        close = find_red_cross_force(image, title_bar_only=False)
        if close and close[0] < 0.50 and close[1] > 0.55:
            driver._tap_forced(*close)
            CONTROL.sleep(0.4)
            return
        cancel = (driver.layout.get("buttons") or {}).get("picker_cancel") or [0.27, 0.789]
        logger.info("Закрываю пикер орудий красной печатью ({:.3f}, {:.3f})", float(cancel[0]), float(cancel[1]))
        driver._tap_forced(float(cancel[0]), float(cancel[1]))
        CONTROL.sleep(0.4)

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
        return self._close_presets(driver)

    def _apply_preset_only(self, driver: Any) -> bool:
        if not self._open_presets(driver):
            return False
        ok = self._tap_apply_all(driver)
        CONTROL.sleep(0.35)
        check = find_dialog_green_check(driver._image())
        if check and is_save_preset_dialog(driver._image()):
            driver._tap_norm_exact(*check)
            CONTROL.sleep(0.35)
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
        CONTROL.sleep(0.3)
        if saved:
            self._tap_apply_all(driver)
            CONTROL.sleep(0.3)
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
        point = find_save_preset_button(image)
        if point is None:
            fallback = (driver.layout.get("buttons") or {}).get("preset_save") or [0.28, 0.64]
            point = (float(fallback[0]), float(fallback[1]))
        logger.info("Сохраняю выбранную волну как предустановку ({:.3f}, {:.3f})", point[0], point[1])
        driver._tap_norm_exact(*point)
        CONTROL.sleep(0.4)
        confirm = driver._wait_for(is_save_preset_dialog, timeout=3, label="сохранить предустановку")
        if confirm is None and is_save_preset_dialog(driver._image()):
            confirm = driver._image()
        if confirm is not None:
            check = find_dialog_green_check(confirm) or find_autoselect_confirm(confirm)
            if check is None:
                check = (0.72, 0.72)
            logger.info("Галочка «Сохранить» ({:.3f}, {:.3f})", check[0], check[1])
            driver._tap_norm_exact(*check)
            CONTROL.sleep(0.4)
        return True

    def _tap_apply_all(self, driver: Any) -> bool:
        image = driver._image()
        point = find_apply_preset_all(image)
        if point is None:
            fallback = (driver.layout.get("buttons") or {}).get("preset_apply_all") or [0.52, 0.84]
            point = (float(fallback[0]), float(fallback[1]))
        logger.info("Применяю предустановку ко всем волнам ({:.3f}, {:.3f})", point[0], point[1])
        driver._tap_norm_exact(*point)
        CONTROL.sleep(0.4)
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
        CONTROL.sleep(0.45)
        latest = driver._image()
        return is_formation_screen(latest) and not is_presets_dialog(latest)

    def _run_autoselect(self, driver: Any) -> bool:
        self._dismiss_tool_picker(driver)
        CONTROL.sleep(0.35)
        self._dismiss_tool_picker(driver)
        image = driver._wait_for(is_formation_screen, timeout=8, label="план перед автоподбором")
        if image is None:
            image = driver._image()
        if not is_formation_screen(image):
            # Plan chrome still present even if parchment heuristics flicker.
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
            # The visible red flag extends lower than its active hitbox in BlueStacks.
            # Stay in the upper half; clicks near y=.963 are rendered on the icon but ignored.
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
            CONTROL.sleep(0.35)
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
            CONTROL.sleep(0.5)
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
        logger.warning("Выхожу из плана самураев, чтобы не сохранить юнитов в пресет")
        driver.close_formation_plan()

    def _finish_event(self, driver: Any, sent: int) -> str:
        summary = driver.store.session_summary()
        logger.info("Вторжение самураев закрыто: {} атак, золото {}, рубины {}", sent, summary["gold"], summary["rubies"])
        driver.telegram.report_samurai_complete(
            attacks=sent,
            gold=int(summary["gold"]),
            rubies=int(summary["rubies"]),
        )
        driver.store.skip_mode(self.spec_id)
        return "samurai_complete"


def wave_has_units(ratio: tuple[int, int] | None) -> bool:
    return bool(ratio and ratio[0] > 0)
