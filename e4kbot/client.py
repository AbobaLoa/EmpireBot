from __future__ import annotations

import json
import random
import re
import time
from typing import Any

from loguru import logger
from PIL import ImageDraw

from e4kbot.bluestacks import AdbClient, capture_game_image, save_shot
from e4kbot.paths import LAYOUTS_DIR, ROOT
from e4kbot.safety import commander_number_ok, concurrent_ok, tap_jitter, triangular_delay
from e4kbot.state import StateStore
from e4kbot.telegram_bot import TelegramReporter
from e4kbot.vision import (
    choose_movement,
    choose_nearest_main_castle,
    crop_rel,
    find_picker_cards,
    find_picker_confirm_button,
    find_main_castle_marker,
    find_robber_candidates,
    find_target_attack_button,
    flank_fill_allowed,
    is_formation_screen,
    is_burning_candidate,
    is_map_screen,
    is_travel_dialog,
    movement_confirm_diagnostics,
    ocr_text,
    parse_count,
    parse_coordinate_pair,
    parse_ratio,
    picker_confirm_diagnostics,
    popup_action,
    project_map_coordinate,
)


def load_layout(name: str) -> dict[str, Any]:
    path = LAYOUTS_DIR / f"{name}.json"
    if not path.exists():
        path = LAYOUTS_DIR / "default.json"
    return json.loads(path.read_text(encoding="utf-8"))


def load_targets() -> dict[str, list[dict[str, int]]]:
    path = ROOT / "targets.json"
    if not path.exists():
        return {"barons": [], "nomads": [], "shogun": []}
    return json.loads(path.read_text(encoding="utf-8"))


def _abs_point(image_size: tuple[int, int], rel: list[float]) -> tuple[int, int]:
    w, h = image_size
    return int(rel[0] * w), int(rel[1] * h)


def _crop(image: Any, rel: list[float]) -> Any:
    return crop_rel(image, rel)


def _ocr(image: Any) -> str:
    return ocr_text(image)


def parse_commander_number(text: str) -> int | None:
    match = re.search(r"(\d{1,2})", text.replace("O", "0"))
    if not match:
        return None
    return int(match.group(1))


def parse_march_seconds(text: str) -> int | None:
    text = text.strip()
    match = re.search(r"(\d{1,2})[^\d]+(\d{2})[^\d]+(\d{2})", text)
    if match:
        h, m, s = (int(match.group(1)), int(match.group(2)), int(match.group(3)))
        return h * 3600 + m * 60 + s
    match = re.search(r"(\d{1,2})[^\d]+(\d{2})", text)
    if match:
        return int(match.group(1)) * 60 + int(match.group(2))
    return None


class BlueStacksEngine:
    def __init__(
        self,
        config: dict[str, Any],
        store: StateStore,
        telegram: TelegramReporter,
        adb: AdbClient,
    ) -> None:
        self.config = config
        self.store = store
        self.telegram = telegram
        self.adb = adb
        self.layout = load_layout(str((config.get("bluestacks") or {}).get("layout") or "default"))
        self._next_commander = 1
        self._selected_target_coords: tuple[int, int] | None = None

    def _size(self) -> tuple[int, int]:
        image = capture_game_image(self.config, self.adb)
        if image is None:
            raise RuntimeError("Нет скрина BlueStacks — открой игру")
        return image.size

    def tap_rel(self, key: str) -> None:
        point = self.layout["buttons"][key]
        self._tap_norm(float(point[0]), float(point[1]))

    def _tap_norm(self, nx: float, ny: float) -> None:
        size = self._size()
        x, y = _abs_point(size, [nx, ny])
        jx, jy = tap_jitter(5)
        self.adb.tap(x + jx, y + jy)
        time.sleep(random.uniform(0.25, 0.7))

    def _tap_norm_exact(self, nx: float, ny: float) -> None:
        size = self._size()
        x, y = _abs_point(size, [nx, ny])
        self.adb.tap(x, y)
        time.sleep(0.5)

    def diagnose_unit_picker_confirm(self, click: bool = False) -> tuple[bool, str, Any | None]:
        """Detect/annotate picker confirmation; detection-only unless click=True."""
        image = self._image()
        diagnostic = picker_confirm_diagnostics(image)
        annotated = image.copy()
        draw = ImageDraw.Draw(annotated)
        left, top, right, bottom = diagnostic["popup_bounds"]
        draw.rectangle(
            (
                round(left * image.width),
                round(top * image.height),
                round(right * image.width),
                round(bottom * image.height),
            ),
            outline=(255, 210, 0),
            width=4,
        )
        point = diagnostic["point"]
        if point is not None:
            x, y = _abs_point(image.size, point)
            color = (0, 255, 0) if diagnostic["valid"] else (255, 0, 0)
            draw.ellipse((x - 24, y - 24, x + 24, y + 24), outline=color, width=6)
            draw.line((x - 35, y, x + 35, y), fill=color, width=3)
            draw.line((x, y - 35, x, y + 35), fill=color, width=3)
        save_shot(annotated, "unit-picker-confirm-before.png")
        logger.info("Unit picker confirm diagnostic: {}", diagnostic)
        if not diagnostic["valid"] or point is None:
            return False, "unit_picker_confirm_not_confident", None
        if not click:
            return True, "diagnostic_only", image
        self._tap_norm_exact(*point)
        after = self._wait_for(self._is_plain_formation, timeout=5)
        if after is None or is_map_screen(self._image()):
            save_shot(self._image(), "unit-picker-confirm-transition-failed.png")
            return False, "unit_picker_confirm_transition_failed", None
        units = self._read_ratio_from_image(after, "formation_units")
        if not units or units[0] <= 0:
            save_shot(after, "unit-picker-confirm-fill-not-retained.png")
            return False, "unit_picker_fill_not_retained", None
        save_shot(after, "unit-picker-confirm-after.png")
        return True, "confirmed", after

    def diagnose_movement_confirm(self, click: bool = False) -> tuple[bool, str, Any | None]:
        """Validate the distinct final movement confirmation before sending."""
        image = self._image()
        diagnostic = movement_confirm_diagnostics(image)
        annotated = image.copy()
        draw = ImageDraw.Draw(annotated)
        left, top, right, bottom = diagnostic["dialog_bounds"]
        draw.rectangle(
            (
                round(left * image.width),
                round(top * image.height),
                round(right * image.width),
                round(bottom * image.height),
            ),
            outline=(255, 210, 0),
            width=4,
        )
        point = diagnostic["point"]
        if point is not None:
            x, y = _abs_point(image.size, point)
            color = (0, 255, 0) if diagnostic["valid"] else (255, 0, 0)
            draw.ellipse((x - 24, y - 24, x + 24, y + 24), outline=color, width=6)
            draw.line((x - 35, y, x + 35, y), fill=color, width=3)
            draw.line((x, y - 35, x, y + 35), fill=color, width=3)
        save_shot(annotated, "movement-confirm-before.png")
        logger.info("Movement confirm diagnostic: {}", diagnostic)
        if not diagnostic["valid"] or point is None:
            return False, "movement_confirm_not_confident", None
        if not click:
            return True, "diagnostic_only", image
        self._tap_norm_exact(*point)
        after = self._wait_for(is_map_screen, timeout=8)
        if after is None or is_formation_screen(self._image()):
            failed = self._image()
            save_shot(failed, "movement-confirm-transition-failed.png")
            return False, "movement_confirm_transition_failed", None
        save_shot(after, "movement-confirm-after.png")
        return True, "confirmed", after

    def _swipe_norm(
        self,
        start: tuple[float, float],
        finish: tuple[float, float],
    ) -> None:
        width, height = self._size()
        self.adb.swipe(
            round(start[0] * width),
            round(start[1] * height),
            round(finish[0] * width),
            round(finish[1] * height),
        )
        time.sleep(0.6)

    def dismiss_popups(self) -> None:
        extra = self.layout.get("dismiss") or []
        for point in extra:
            self._tap_norm(float(point[0]), float(point[1]))
        if "close" in self.layout.get("buttons", {}):
            self.tap_rel("close")

    def read_region(self, key: str) -> str:
        image = capture_game_image(self.config, self.adb)
        if image is None:
            return ""
        region = self.layout.get("regions", {}).get(key)
        if not region:
            return ""
        return _ocr(_crop(image, region))

    def _image(self) -> Any:
        image = capture_game_image(self.config, self.adb)
        if image is None:
            raise RuntimeError("Нет скрина BlueStacks — открой игру")
        return image

    def _wait_for(self, predicate: Any, timeout: float | None = None) -> Any | None:
        timeout = float(
            timeout
            or (self.config.get("vision") or {}).get("screen_timeout_seconds")
            or 8
        )
        deadline = time.time() + timeout
        while time.time() < deadline:
            image = self._image()
            if predicate(image):
                return image
            time.sleep(0.35)
        return None

    def ensure_map(self) -> Any | None:
        """Reach the world map while only dismissing recognized blockers."""
        retries = int((self.config.get("vision") or {}).get("popup_retries") or 4)
        for _ in range(retries):
            image = self._image()
            if is_map_screen(image):
                candidates = find_robber_candidates(
                    image,
                    float((self.config.get("vision") or {}).get("robber_threshold") or 0.65),
                )
                if candidates:
                    return image
                action = popup_action(image)
                if action:
                    logger.info("Замков не видно: закрываю распознанное окно")
                    self._tap_norm(*action)
                    time.sleep(0.8)
                    continue
                return image
            if is_travel_dialog(image):
                self.tap_rel("travel_cancel")
            elif is_formation_screen(image):
                self.tap_rel("formation_close")
            else:
                action = popup_action(image)
                if action:
                    self._tap_norm(*action)
                else:
                    self.tap_rel("map")
            time.sleep(0.9)
        image = self._image()
        return image if is_map_screen(image) else None

    def _select_visible_target(self, image: Any, kind: str) -> tuple[float, float] | None:
        threshold = float((self.config.get("vision") or {}).get("robber_threshold") or 0.65)
        candidates = find_robber_candidates(image, threshold)
        if not candidates:
            return None
        main_region = self.layout.get("regions", {}).get("main_castle_coords")
        x_region = self.layout.get("regions", {}).get("viewport_x")
        y_region = self.layout.get("regions", {}).get("viewport_y")
        main = (
            parse_coordinate_pair(ocr_text(crop_rel(image, main_region), psm=6))
            if main_region
            else None
        )
        viewport_x = (
            parse_count(ocr_text(crop_rel(image, x_region), psm=6)) if x_region else None
        )
        viewport_y = (
            parse_count(ocr_text(crop_rel(image, y_region), psm=6)) if y_region else None
        )
        if main is None or viewport_x is None or viewport_y is None:
            logger.warning("Не прочитаны координаты главного замка или карты")
            return None
        vision = self.config.get("vision") or {}
        anchor_raw = vision.get("map_anchor") or [0.50, 0.54]
        scale_raw = vision.get("map_coordinate_scale") or [0.044, 0.044]
        kingdom = int((self.config.get("baron_attacks") or {}).get("kingdom", 0))
        projected: dict[tuple[float, float], tuple[int, int]] = {}
        eligible: list[tuple[float, float, float]] = []
        cooling = 0
        burning = 0
        for candidate in candidates:
            if is_burning_candidate(image, (candidate[0], candidate[1])):
                burning += 1
                continue
            coords = project_map_coordinate(
                (candidate[0], candidate[1]),
                (viewport_x, viewport_y),
                (float(anchor_raw[0]), float(anchor_raw[1])),
                (float(scale_raw[0]), float(scale_raw[1])),
            )
            stable = (round(coords[0]), round(coords[1]))
            projected[(candidate[0], candidate[1])] = stable
            if self.store.target_available(kind, kingdom, stable[0], stable[1]):
                eligible.append(candidate)
            else:
                cooling += 1
        main_marker = find_main_castle_marker(image)
        if main_marker:
            chosen_candidate = min(
                eligible,
                key=lambda candidate: (candidate[0] - main_marker[0]) ** 2
                + (candidate[1] - main_marker[1]) ** 2,
                default=None,
            )
            chosen = (
                (chosen_candidate[0], chosen_candidate[1])
                if chosen_candidate is not None
                else None
            )
        else:
            chosen = choose_nearest_main_castle(
                eligible,
                main,
                (viewport_x, viewport_y),
                (float(anchor_raw[0]), float(anchor_raw[1])),
                (float(scale_raw[0]), float(scale_raw[1])),
            )
        if chosen is None:
            logger.info(f"Все видимые замки на перезарядке: {cooling}")
            return None
        self._selected_target_coords = projected[chosen]
        logger.info(
            f"Найдено замков разбойников: {len(candidates)}, горят: {burning}, "
            f"на локальной перезарядке: {cooling}; "
            f"главный замок {main[0]}:{main[1]}, карта {viewport_x}:{viewport_y}; "
            f"выбрана цель {self._selected_target_coords[0]}:{self._selected_target_coords[1]}"
        )
        return chosen

    def _open_formation(self, point: tuple[float, float], kind: str) -> bool:
        self._tap_norm(*point)
        popup = self._wait_for(lambda image: find_target_attack_button(image) is not None, timeout=5)
        if popup is None:
            blocked = self._image()
            action = popup_action(blocked)
            if action:
                logger.info("Цель перекрыта окном; закрываю его перед повтором")
                self._tap_norm(*action)
            return False
        x_region = self.layout.get("regions", {}).get("viewport_x")
        y_region = self.layout.get("regions", {}).get("viewport_y")
        target_x = parse_count(ocr_text(crop_rel(popup, x_region), psm=6)) if x_region else None
        target_y = parse_count(ocr_text(crop_rel(popup, y_region), psm=6)) if y_region else None
        if target_x is None or target_y is None:
            return False
        self._selected_target_coords = (target_x, target_y)
        kingdom = int((self.config.get("baron_attacks") or {}).get("kingdom", 0))
        if not self.store.target_available(kind, kingdom, target_x, target_y):
            logger.info(f"Цель {target_x}:{target_y} ещё на локальной перезарядке")
            self.tap_rel("map")
            return False
        attack_point = find_target_attack_button(popup)
        if attack_point is None:
            return False
        self._tap_norm(*attack_point)
        time.sleep(1.0)
        self.tap_rel("start_attack_confirm")
        return self._wait_for(is_formation_screen) is not None

    def _read_ratio(self, key: str) -> tuple[int, int] | None:
        return parse_ratio(self.read_region(key))

    def _is_plain_formation(self, image: Any) -> bool:
        return is_formation_screen(image) and not find_picker_cards(image)

    def _select_best_picker_card(self) -> bool:
        """Select one detected card with strict progress and time bounds."""
        timeout = float((self.config.get("vision") or {}).get("picker_timeout_seconds") or 15)
        deadline = time.time() + timeout
        image = self._image()
        before = self._read_ratio_from_image(image, "picker_units")
        if not before or time.time() >= deadline:
            return False
        if before[0] >= before[1] > 0:
            return True
        cards = [
            card
            for card in find_picker_cards(image)
            if int(card["available"]) > int(card["selected"])
        ]
        if not cards:
            return False
        chosen = max(cards, key=lambda card: int(card["available"]) - int(card["selected"]))
        # Re-read immediately before acting; stale coordinates are never used.
        fresh = self._image()
        fresh_cards = find_picker_cards(fresh)
        matching = [
            card
            for card in fresh_cards
            if card["fingerprint"] == chosen["fingerprint"]
            and int(card["available"]) > int(card["selected"])
        ]
        if not matching or time.time() >= deadline:
            return False
        card = matching[0]
        self._tap_norm(float(card["point"][0]), float(card["point"][1]))
        after_image = self._image()
        after = self._read_ratio_from_image(after_image, "picker_units")
        for _ in range(4):
            if after is not None or self._is_plain_formation(after_image):
                break
            time.sleep(0.3)
            after_image = self._image()
            after = self._read_ratio_from_image(after_image, "picker_units")
        logger.info(f"Выбор юнитов в ячейке: {before} -> {after}")
        if after is None and self._is_plain_formation(after_image):
            time.sleep(1.0)
            return True
        if after is None and find_picker_confirm_button(after_image) is not None:
            return True
        return bool(after and (after[0] > before[0] or after[0] >= after[1] > 0))

    def _prepare_single_center_wave(self) -> tuple[bool, str]:
        # The game opens with an empty first wave and the centre/front selected.
        # Do not touch the clear-wave or flank controls.
        ratio = self._read_ratio("formation_units")
        if ratio and ratio[0] == ratio[1] and ratio[1] > 0:
            tools = self._read_ratio("formation_tools")
            return (bool(tools and tools[0] == 0), "tools_not_empty")

        formation = self._image()
        max_actions = int((self.config.get("vision") or {}).get("picker_max_actions") or 4)
        slot_keys = ("unit_slot", "unit_slot_second")[:max_actions]
        for slot_key in slot_keys:
            units = self._read_ratio_from_image(formation, "formation_units")
            if units and units[0] == units[1]:
                break
            self.tap_rel(slot_key)
            picker = self._wait_for(
                lambda image: self._read_ratio_from_image(image, "picker_units") is not None,
                timeout=5,
            )
            if picker is None:
                return False, "unit_picker_not_found"
            picker_ratio = self._read_ratio_from_image(picker, "picker_units")
            if not picker_ratio or picker_ratio[1] <= 0:
                return False, "center_capacity_not_read"
            if not self._select_best_picker_card():
                save_shot(self._image(), "unit-picker-selection-no-progress.png")
                return False, "unit_picker_selection_no_progress"
            current_screen = self._image()
            if self._is_plain_formation(current_screen):
                formation = current_screen
                continue
            confirmed, reason, formation = self.diagnose_unit_picker_confirm(click=True)
            if not confirmed or formation is None:
                return False, reason
        final_units = self._read_ratio_from_image(formation, "formation_units")
        final_tools = self._read_ratio_from_image(formation, "formation_tools")
        if not final_units or final_units[1] <= 0:
            save_shot(formation, "formation-verification-failed.png")
            return False, "center_capacity_not_read"
        if not final_tools or final_tools[0] != 0:
            save_shot(formation, "formation-verification-failed.png")
            return False, "tools_not_empty"
        fill_ratio = final_units[0] / final_units[1]
        minimum = float((self.config.get("vision") or {}).get("minimum_flank_fill") or 0.70)
        if not flank_fill_allowed(final_units[0], final_units[1], minimum):
            percentage = round(fill_ratio * 100)
            message = (
                f"Солдаты кончились: центральный фланг "
                f"{final_units[0]}/{final_units[1]} ({percentage}%)"
            )
            self.telegram.report_status(f"⚠️ {message}")
            self.store.live.last_error = message
            self.store.save()
            save_shot(formation, "formation-verification-failed.png")
            return False, "soldiers_depleted"
        logger.info(
            f"Центральный фланг: {final_units[0]}/{final_units[1]} "
            f"({fill_ratio:.0%}), орудия 0"
        )
        return True, ""

    def _read_ratio_from_image(self, image: Any, key: str) -> tuple[int, int] | None:
        region = self.layout.get("regions", {}).get(key)
        return parse_ratio(ocr_text(crop_rel(image, region))) if region else None

    def _movement_option(self, image: Any) -> tuple[str, int | None]:
        region = self.layout.get("regions", {}).get("feather_count")
        feathers = parse_count(ocr_text(crop_rel(image, region), psm=6)) if region else None
        movement = choose_movement(feathers)
        if movement == "unknown":
            return movement, None
        if movement == "feather":
            self.tap_rel("feather_option")
            return "feather", feathers
        # Zero feathers: the gold movement option is already selected by
        # default. Do not change the horse/tile; confirm the dialog as-is.
        return "gold", feathers

    def search_and_attack(self, kind: str, target: dict[str, int]) -> str:
        dry_run = bool(self.config.get("dry_run", True))
        in_flight = len(self.store.in_flight())
        ok_conc, msg = concurrent_ok(in_flight, self.config)
        if not ok_conc:
            return "wait_return"

        self.dismiss_popups()
        time.sleep(random.uniform(0.3, 0.8))
        self.tap_rel("map")
        time.sleep(random.uniform(0.6, 1.2))
        self.tap_rel("search")
        time.sleep(random.uniform(0.4, 0.9))
        x, y = int(target["x"]), int(target["y"])
        self.tap_rel("coord_x")
        self.adb.text(str(x))
        self.tap_rel("coord_y")
        self.adb.text(str(y))
        self.tap_rel("search_go")
        time.sleep(random.uniform(1.2, 2.0))
        self.tap_rel("target_center")
        time.sleep(random.uniform(0.6, 1.1))
        self.tap_rel("attack")
        time.sleep(random.uniform(0.8, 1.4))
        return self._finish_attack(kind, target)

    def on_screen_attack(self, kind: str) -> str:
        opened = False
        attempts = int((self.config.get("vision") or {}).get("popup_retries") or 4) + 1
        for _ in range(attempts):
            image = self.ensure_map()
            if image is None:
                time.sleep(0.6)
                continue
            point = self._select_visible_target(image, kind)
            if point is None:
                action = popup_action(image)
                if action:
                    self._tap_norm(*action)
                    time.sleep(0.8)
                    continue
                logger.info("На карте нет доступных замков разбойников")
                return "no_targets"
            if self._open_formation(point, kind):
                opened = True
                break
            time.sleep(0.8)
        if not opened:
            logger.warning("Не открылся экран формирования атаки")
            return "formation_not_found"
        ok, reason = self._prepare_single_center_wave()
        if not ok:
            self.store.live.last_error = reason
            self.store.save()
            self.tap_rel("formation_close")
            logger.warning(f"Атака отменена: {reason}")
            return "unsafe_formation"
        self.tap_rel("formation_attack")
        travel = self._wait_for(is_travel_dialog)
        if travel is None:
            self.tap_rel("formation_close")
            return "travel_dialog_not_found"
        movement, feathers = self._movement_option(travel)
        if movement == "unknown":
            self.tap_rel("travel_cancel")
            logger.warning("Атака отменена: число перьев не распознано")
            return "feather_count_not_read"
        time.sleep(0.5)
        travel = self._image()
        one_way = self._read_march_time(travel)
        if one_way is None:
            self.tap_rel("travel_cancel")
            logger.warning("Атака отменена: не удалось прочитать время похода")
            return "march_time_not_read"
        if movement == "gold":
            logger.warning(f"Перьев нет ({feathers}); выбран разрешённый вариант за золото")
        fake_target = {
            "kingdom": int((self.config.get("baron_attacks") or {}).get("kingdom", 0)),
            "x": int(self._selected_target_coords[0] if self._selected_target_coords else point[0] * 1000),
            "y": int(self._selected_target_coords[1] if self._selected_target_coords else point[1] * 1000),
        }
        return self._finish_attack(kind, fake_target, one_way, movement)

    def _read_march_time(self, image: Any) -> int | None:
        region = self.layout.get("regions", {}).get("travel_duration")
        if not region:
            return None
        return parse_march_seconds(ocr_text(crop_rel(image, region)))

    def _finish_attack(
        self,
        kind: str,
        target: dict[str, int],
        one_way: int | None = None,
        movement: str = "",
    ) -> str:
        dry_run = bool(self.config.get("dry_run", True))
        kid = int(target.get("kingdom", 0))
        x, y = int(target["x"]), int(target["y"])
        commander_no = parse_commander_number(self.read_region("commander_number"))
        if commander_no is None:
            commander_no = self._next_commander
        ok_cmd, cmd_msg = commander_number_ok(commander_no, self.config)
        if not ok_cmd:
            self.store.live.stopped_reason = cmd_msg
            self.telegram.report_stop(cmd_msg)
            self.tap_rel("close")
            return "stop"
        self._next_commander = int(commander_no) + 1
        one_way = one_way or parse_march_seconds(self.read_region("march_time"))
        if one_way is None:
            self.tap_rel("travel_cancel")
            return "march_time_not_read"
        shot = capture_game_image(self.config, self.adb)
        shot_path = None
        if shot is not None:
            shot_path = save_shot(shot, f"{kind}_{x}_{y}_{int(time.time())}.png")
        if dry_run:
            valid, reason, _ = self.diagnose_movement_confirm(click=False)
            if not valid:
                self.store.live.last_error = reason
                self.store.save()
                return reason
            self.tap_rel("travel_cancel")
            time.sleep(0.5)
            self.tap_rel("formation_close")
            self.telegram.report_status(
                f"🧪 DRY-RUN отменён перед отправкой: {kind} K{kid} ({x}, {y}), "
                f"время {one_way} сек"
            )
            return kind
        confirmed, reason, _ = self.diagnose_movement_confirm(click=True)
        if not confirmed:
            self.store.live.last_error = reason
            self.store.save()
            logger.error("Атака не зарегистрирована: {}", reason)
            return reason
        march = self.store.register_march(
            commander_no,
            commander_no,
            kind,
            kid,
            x,
            y,
            one_way,
            str(shot_path) if shot_path else "",
            movement=movement,
        )
        self.telegram.report_attack(
            self.store.live.account or "BlueStacks",
            kind,
            kid,
            x,
            y,
            commander_no,
            one_way,
            int(march.return_at - march.sent_at),
            shot_path,
            dry_run=False,
        )
        delay = triangular_delay(self.config.get("attack_delay_seconds") or [4, 10])
        self.store.live.next_attack_at = time.time() + delay
        self.store.save()
        time.sleep(delay)
        return kind

    def run_cycle(self) -> str:
        kind = str(self.config.get("current_target_kind") or "baron")
        style = str(self.config.get("attack_style") or "on_screen")
        if style == "search_coords":
            targets = load_targets()
            rows = targets.get({"baron": "barons", "nomad": "nomads", "shogun": "shogun"}.get(kind, "barons")) or []
            if not rows:
                logger.info("targets.json пуст — для поиска по координатам добавь цели")
                return "no_targets"
            sent = 0
            for target in rows:
                if not concurrent_ok(len(self.store.in_flight()), self.config)[0]:
                    return "wait_return"
                result = self.search_and_attack(kind, target)
                if result == "stop":
                    return "stop"
                sent += 1
            return f"client:{sent}"

        if not concurrent_ok(len(self.store.in_flight()), self.config)[0]:
            return "wait_return"
        result = self.on_screen_attack(kind)
        if result == "stop":
            return "stop"
        if result != kind:
            return result
        return f"client:1"
