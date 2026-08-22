from __future__ import annotations

import re
from typing import Any

from loguru import logger

from e4kbot.bluestacks import save_shot
from e4kbot.control import CONTROL
from e4kbot.safety import concurrent_ok
from e4kbot.client import HuntTarget
from e4kbot.vision import (
    crop_rel,
    find_apply_preset_all,
    find_autoselect_button,
    find_autoselect_confirm,
    find_formation_attack_button,
    find_preset_button,
    find_preset_dialog_close,
    find_red_cross_force,
    find_reward_confirm,
    find_target_attack_button,
    find_tool_bonus_candidates,
    find_tool_slider_plus,
    is_autoselect_dialog,
    is_difficulty_dialog,
    is_event_reward_popup,
    is_formation_screen,
    is_map_screen,
    is_no_commanders_parchment,
    is_presets_dialog,
    is_special_offers_screen,
    is_travel_dialog,
    ocr_text,
    ocr_text_ui,
    parse_count,
    parse_samurai_camp_level,
    pick_open_difficulty_point,
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
        ok, _ = concurrent_ok(len(driver.store.in_flight()), driver.config)
        if not ok:
            return "wait_return"
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
                logger.info("На карте нет лагерей самураев")
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
                logger.info("Лагерь {} не совпал — беру любой видимый", target.coords or target.point)
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
            return self._execute(driver, (0.50, 0.50))
        if driver._plan_or_picker_open(current) and not is_map_screen(current):
            logger.info("Пикер без плана атаки — закрываю крестиком, не магазин")
            self._dismiss_tool_picker(driver)
            latest = driver._image()
            if is_formation_screen(latest):
                return self._execute(driver, (0.50, 0.50))
        return None

    def _execute(self, driver: Any, point: tuple[float, float]) -> str:
        if driver._dismiss_no_commanders():
            return "no_commanders"
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
            logger.warning("Самураи: подготовка не прошла ({}) — план не закрываю зелёной печатью", reason)
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
        movement, feathers = driver._movement_option(travel)
        if movement == "unknown":
            driver.tap_rel("travel_cancel")
            return "feather_count_not_read"
        CONTROL.sleep(0.4)
        travel = driver._image()
        one_way = driver._read_march_time(travel)
        if one_way is None:
            driver.tap_rel("travel_cancel")
            return "march_time_not_read"
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
                if bool(driver.config.get("samurai_tools_depleted")):
                    logger.info(
                        "Запас выбранного +3% орудия исчерпан — сохраняю частичный пресет и иду в автоподбор"
                    )
                elif units_present:
                    logger.warning(
                        "Орудия не полные, в волне уже юниты — выхожу и зайду без сохранения пресета"
                    )
                    self._leave_plan(driver)
                    return False, "retry_samurai_tools"
                else:
                    filled = self._fill_support_tools(driver)
                    if filled and not self._save_preset_safe(driver):
                        return False, "preset_save_blocked"
            if units_present:
                logger.info("Волны уже заполнены автоподбором — повторно диалог не открываю")
                return True, ""
            if bool(driver.config.get("samurai_waves_ready")):
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
                    return False, "retry_samurai_tools"
                if not self._first_preset_flow(driver):
                    return False, "preset_setup_failed"
                self._preset_ready = True
            else:
                logger.info("Орудий поддержки нет — пресет не сохраняю, сразу автоподбор")
                self._tools_unavailable = True
                self._dismiss_tool_picker(driver)
        if not self._run_autoselect(driver):
            return False, "autoselect_failed"
        return True, ""

    def _tools_full(self, ratio: tuple[int, int] | None) -> bool:
        if not ratio or ratio[1] <= 0:
            return False
        return ratio[0] >= ratio[1]

    def _fill_support_tools(self, driver: Any) -> bool:
        """Fill every flank with the lowest +% tool; one type per flank."""
        buttons = driver.layout.get("buttons") or {}
        flanks = [
            buttons.get("flank_1") or [0.22, 0.50],
            buttons.get("flank_2") or [0.38, 0.50],
            buttons.get("flank_3") or buttons.get("center_flank") or [0.54, 0.50],
        ]
        slot = buttons.get("tool_slot") or [0.62, 0.70]
        filled = 0
        for index, flank in enumerate(flanks, start=1):
            driver._tap_norm_exact(float(flank[0]), float(flank[1]))
            CONTROL.sleep(0.28)
            driver._tap_norm_exact(float(slot[0]), float(slot[1]))
            CONTROL.sleep(0.55)
            image = driver._image()
            try:
                save_shot(image, f"samurai_tools_flank_{index}.png")
            except Exception:
                pass
            bonuses = self._scan_tool_bonuses(driver, index)
            if not bonuses:
                logger.info("Фланг {}: +% не найдены мелким скроллом — орудия и пресет пропускаю", index)
                self._dismiss_tool_picker(driver)
                if index == 1:
                    return False
                continue
            if self._chosen_tool_percent is None:
                self._chosen_tool_percent = int(min(bonuses, key=lambda item: item[0])[0])
            target_pct = int(self._chosen_tool_percent)
            visible = [
                item for item in find_tool_bonus_candidates(driver._image()) if item[0] == target_pct
            ]
            best = visible[0] if visible else self._seek_tool_percent(driver, target_pct, index)
            if best is None:
                logger.warning("Фланг {}: +{}% не нашёл на экране после прокрутки", index, target_pct)
                self._dismiss_tool_picker(driver)
                continue
            logger.info(
                "Орудие фланга {}: +{}% (меньше лучше) ({:.3f}, {:.3f})",
                index,
                best[0],
                best[1],
                best[2],
            )
            if not self._fill_selected_tool(driver, best[2]):
                logger.warning("Фланг {}: не заполнил слот орудий без магазина", index)
                self._dismiss_tool_picker(driver)
                if index == 1:
                    logger.info("Запас орудий 0 — набор и пресет пропускаю, без магазина")
                    return False
                continue
            ratio = driver._read_ratio_from_image(driver._image(), "picker_units")
            ok, reason, _ = driver.diagnose_unit_picker_confirm(click=True, observed_fill=ratio)
            if not ok:
                logger.warning("Пикер орудий фланга {}: {}", index, reason)
                continue
            formation = driver._wait_for(is_formation_screen, timeout=6, label="план после орудий")
            if formation is None:
                continue
            filled += 1
        image = driver._image()
        tools = driver._read_ratio_from_image(image, "formation_tools")
        logger.info("Орудия после всех фронтов: {} (флангов {})", tools, filled)
        return filled >= 1

    def _scan_tool_bonuses(self, driver: Any, flank: int) -> list[tuple[int, float, float]]:
        """Match +% after each small scroll. Never assume the whole list fits on one screen."""
        pages: list[list[tuple[int, float, float]]] = []
        seen: set[int] = set()
        stagnant = 0
        for page in range(12):
            image = driver._image()
            found = find_tool_bonus_candidates(image)
            pages.append(found)
            percents = {item[0] for item in found}
            logger.info("Пикер орудий фланг {} шаг {}: +% {}", flank, page + 1, sorted(percents))
            if self._chosen_tool_percent is not None and self._chosen_tool_percent in percents:
                return merge_tool_bonus_pages(pages)
            new = percents - seen
            seen |= percents
            if not new and page > 0:
                stagnant += 1
                if stagnant >= 3:
                    break
            else:
                stagnant = 0
            if page == 11:
                break
            driver.scroll_tool_inventory()
        return merge_tool_bonus_pages(pages)

    def _seek_tool_percent(
        self,
        driver: Any,
        percent: int,
        flank: int,
    ) -> tuple[int, float, float] | None:
        """Re-open the list from the top and scroll until the chosen +% is visible."""
        self._dismiss_tool_picker(driver)
        slot = (driver.layout.get("buttons") or {}).get("tool_slot") or [0.62, 0.70]
        driver._tap_norm_exact(float(slot[0]), float(slot[1]))
        CONTROL.sleep(0.55)
        for page in range(12):
            image = driver._image()
            found = [item for item in find_tool_bonus_candidates(image) if item[0] == percent]
            if found:
                logger.info("Фланг {}: +{}% снова на экране после шага {}", flank, percent, page + 1)
                return found[0]
            driver.scroll_tool_inventory()
        return None

    def _fill_selected_tool(self, driver: Any, row_y: float) -> bool:
        """Tap the slider + until 0/20 becomes full. Never the ruby cart."""
        image = driver._image()
        before = driver._read_ratio_from_image(image, "picker_units")
        plus = find_tool_slider_plus(image, row_y)
        logger.info("Плюс слайдера орудий ({:.3f}, {:.3f}), было {}", plus[0], plus[1], before)
        for _ in range(35):
            driver._tap_norm_exact(*plus)
            CONTROL.sleep(0.22)
            ratio = driver._read_ratio_from_image(driver._image(), "picker_units")
            if ratio and ratio[1] > 0 and ratio[0] >= ratio[1]:
                logger.info("Орудия пикера заполнены {}", ratio)
                return True
            if ratio and before and ratio[0] > before[0]:
                before = ratio
        ratio = driver._read_ratio_from_image(driver._image(), "picker_units")
        return bool(ratio and ratio[1] > 0 and ratio[0] > 0)

    def _dismiss_tool_picker(self, driver: Any) -> None:
        image = driver._image()
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
        CONTROL.sleep(0.4)
        if not self._tap_apply_all(driver):
            self._close_presets(driver)
            return False
        CONTROL.sleep(0.4)
        self._tap_save_preset(driver)
        CONTROL.sleep(0.35)
        return self._close_presets(driver)

    def _apply_preset_only(self, driver: Any) -> bool:
        if not self._open_presets(driver):
            return False
        ok = self._tap_apply_all(driver)
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
        CONTROL.sleep(0.35)
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
