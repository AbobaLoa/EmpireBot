from __future__ import annotations

import json
import random
import re
import time
from dataclasses import dataclass
from typing import Any

from loguru import logger
from PIL import ImageDraw

from e4kbot.bluestacks import AdbClient, capture_game_image, save_shot
from e4kbot.control import CONTROL
from e4kbot.paths import LAYOUTS_DIR, ROOT
from e4kbot.runtime.live import emit
from e4kbot.safety import (
    commander_number_ok,
    concurrent_ok,
    mark_successful_send,
    tap_jitter,
    wait_for_send_slot,
)
from e4kbot.state import StateStore
from e4kbot.telegram_bot import TelegramReporter
from e4kbot.vision import (
    choose_movement,
    crop_rel,
    _no_commanders_red_closes,
    find_empty_wave_warning_confirm,
    find_picker_cards,
    find_picker_confirm_button,
    find_picker_max_control,
    find_main_castle_marker,
    find_reconnect_button,
    find_reward_confirm,
    find_robber_candidates,
    find_samurai_candidates,
    find_formation_attack_button,
    find_red_cross_force,
    find_target_attack_button,
    find_parchment_title_close,
    flank_fill_allowed,
    is_connection_error_dialog,
    is_formation_screen,
    is_burning_candidate,
    is_difficulty_dialog,
    is_event_reward_popup,
    is_green_hire_point,
    is_hire_menu,
    is_inbox_screen,
    is_loading_screen,
    is_map_screen,
    is_no_commanders_parchment,
    is_offer_rail_point,
    is_special_offers_screen,
    is_taxes_dialog,
    is_travel_dialog,
    movement_confirm_diagnostics,
    no_commanders_diagnostics,
    ocr_text,
    ocr_text_ui,
    parse_count,
    parse_coordinate_pair,
    parse_ratio,
    parse_samurai_camp_level,
    picker_confirm_diagnostics,
    popup_action,
    project_map_coordinate,
    remaining_attacks_from_level,
    special_offers_close_point,
)

DEFAULT_HUNT_BATCH = 10


@dataclass(frozen=True)
class HuntTarget:
    point: tuple[float, float]
    coords: tuple[int, int] | None = None

    def identity(self) -> tuple[Any, ...]:
        if self.coords is not None:
            return ("xy", int(self.coords[0]), int(self.coords[1]))
        return ("sc", round(self.point[0], 2), round(self.point[1], 2))


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
        self._blocked_screen_targets: list[tuple[float, float]] = []
        self._last_picker_fill: tuple[int, int] | None = None
        self._hunt_queue: list[HuntTarget] = []
        self._picker_stall_count = 0
        self._no_commanders_seen = False

    _PICKER_STALL_REASONS = frozenset(
        {
            "unit_picker_selection_no_progress",
            "unit_picker_not_found",
            "unit_picker_fill_not_retained",
            "unit_picker_confirm_not_confident",
            "unit_picker_confirm_transition_failed",
            "formation_units_empty",
        }
    )

    def _vision_seconds(self, key: str, default: float) -> float:
        vision = (getattr(self, "config", None) or {}).get("vision") or {}
        return float(vision.get(key) or default)

    def _size(self) -> tuple[int, int]:
        image = capture_game_image(self.config, self.adb)
        if image is None:
            raise RuntimeError("Нет скрина BlueStacks — открой игру")
        return image.size

    def _plan_or_picker_open(self, image: Any | None = None) -> bool:
        """True while attack planning / unit picker is on screen."""
        try:
            shot = image if image is not None else self._image()
        except Exception:
            return True
        if is_formation_screen(shot):
            return True
        if find_picker_cards(shot) or find_picker_confirm_button(shot):
            return True
        return False

    def tap_rel(self, key: str) -> None:
        banned = {
            "formation_close",
            "close",
            "picker_cancel",
            "attack_cancel",
            "map",
        }
        if key in banned and self._plan_or_picker_open():
            logger.warning("Не жму крестик/закрытие {} — открыт план атаки", key)
            return
        if key == "search":
            logger.warning("Не жму search — кнопка на рейке спецпредложений/магазина")
            return
        point = self.layout["buttons"][key]
        nx, ny = float(point[0]), float(point[1])
        if key in {"formation_close", "close"} and is_offer_rail_point(nx, ny):
            logger.warning(
                "Не жму {} ({:.3f}, {:.3f}) — это правый rail, не крестик плана",
                key,
                nx,
                ny,
            )
            self._dismiss_special_offers_if_open()
            return
        self._tap_norm(nx, ny)

    def _rail_guard_allows(self, nx: float, ny: float) -> bool:
        """Block the special-offers rail unless this is that overlay's red X or the footer."""
        if ny >= 0.90:
            return True
        if not is_offer_rail_point(nx, ny):
            return True
        try:
            image = self._image()
        except Exception:
            logger.warning("Блокирую тап на rail ({:.3f}, {:.3f}) — нет скрина", nx, ny)
            return False
        if self._plan_or_picker_open(image):
            logger.warning(
                "Блокирую тап на rail ({:.3f}, {:.3f}) — открыт план, не магазин",
                nx,
                ny,
            )
            return False
        if not is_special_offers_screen(image):
            if is_no_commanders_parchment(image) and ny < 0.55:
                return True
            logger.warning(
                "Блокирую тап на правом rail ({:.3f}, {:.3f}) — магазин/сундуки/оплату не открываю",
                nx,
                ny,
            )
            return False
        close_x, close_y = special_offers_close_point(image)
        if abs(nx - close_x) > 0.06 or abs(ny - close_y) > 0.10:
            logger.warning(
                "На спецпредложениях жму только красный крестик, не ({:.3f}, {:.3f})",
                nx,
                ny,
            )
            return False
        return True

    def _dismiss_special_offers_if_open(self, image: Any | None = None) -> bool:
        """If «спецпредложения» is open, close with red X / Escape — never buy/view-offer."""
        try:
            shot = image if image is not None else self._image()
        except Exception:
            return False
        if not is_special_offers_screen(shot):
            return False
        close_x, close_y = special_offers_close_point(shot)
        # Mid-dialog «don't show» X sits near y≈0.57; title-bar X is higher.
        if (
            close_y <= 0.68
            and close_x < 0.92
            and not is_green_hire_point(close_x, close_y)
        ):
            logger.info(
                "Закрываю спецпредложения красным крестиком ({:.3f}, {:.3f})",
                close_x,
                close_y,
            )
            self._tap_forced(close_x, close_y)
            CONTROL.sleep(0.35)
        logger.info("Спецпредложение — Escape/Back, не магазин и не «Смотреть предложение»")
        self.adb.key(4)
        CONTROL.sleep(0.45)
        return True

    def _dismiss_connection_error_if_open(self, image: Any | None = None) -> bool:
        """Tap «СОЕДИНИТЬ ПОВТОРНО» — never SUPPORT."""
        try:
            shot = image if image is not None else self._image()
        except Exception:
            return False
        if self._plan_or_picker_open(shot) or is_formation_screen(shot) or is_travel_dialog(shot):
            return False
        if not is_connection_error_dialog(shot):
            return False
        point = find_reconnect_button(shot)
        logger.info("Ошибка соединения — жму «Соединить повторно» ({:.3f}, {:.3f})", point[0], point[1])
        self._tap_forced(*point)
        CONTROL.sleep(1.0)
        return True

    def _dismiss_reward_popups(self, image: Any | None = None) -> bool:
        """Click green claim checks until reward chain is gone. Never shop/hire."""
        clicked = False
        for _ in range(8):
            try:
                shot = image if image is not None else self._image()
            except Exception:
                break
            image = None
            explicit_event = is_event_reward_popup(shot)
            if not explicit_event and (
                self._plan_or_picker_open(shot)
                or is_formation_screen(shot)
                or is_travel_dialog(shot)
            ):
                break
            if not explicit_event and is_special_offers_screen(shot):
                if self._dismiss_special_offers_if_open(shot):
                    clicked = True
                    continue
                break
            if is_loading_screen(shot):
                break
            point = find_reward_confirm(shot)
            if point is None:
                break
            title = ocr_text_ui(crop_rel(shot, [0.08, 0.14, 0.92, 0.30]), psm=6)
            normalized_title = re.sub(r"[^a-zа-яё]", "", title.lower())
            explicit_reward = explicit_event or any(
                marker in normalized_title
                for marker in ("наград", "reward", "event")
            )
            if is_green_hire_point(*point) and point[0] > 0.58 and not explicit_reward:
                logger.warning("Пропускаю зелёную печать найма ({:.3f}, {:.3f})", point[0], point[1])
                break
            logger.info("Награда — жму зелёную галочку ({:.3f}, {:.3f})", point[0], point[1])
            self._tap_forced(*point)
            clicked = True
            CONTROL.sleep(0.55)
        return clicked

    def _dismiss_hire_menu_if_open(self, image: Any | None = None) -> bool:
        """Close parchment «Нанять» with title X. Never ruby hire."""
        try:
            shot = image if image is not None else self._image()
        except Exception:
            return False
        if self._plan_or_picker_open(shot):
            return False
        if not is_hire_menu(shot):
            return False
        close = find_parchment_title_close(shot) or (0.86, 0.06)
        if close[1] > 0.14 or close[0] >= 0.92:
            close = (0.86, 0.06)
        logger.info("Закрываю меню найма крестиком ({:.3f}, {:.3f})", close[0], close[1])
        self._tap_forced(*close)
        CONTROL.sleep(0.4)
        return True

    def _dismiss_inbox_if_open(self, image: Any | None = None) -> bool:
        """Close mail / «Удалить все» with Back. Never confirm delete, never shop."""
        try:
            shot = image if image is not None else self._image()
        except Exception:
            return False
        if not is_inbox_screen(shot):
            return False
        logger.info("Почта открыта — закрываю Back, письма не удаляю")
        self.adb.key(4)
        CONTROL.sleep(0.45)
        latest = self._image()
        if is_inbox_screen(latest):
            close = find_parchment_title_close(latest)
            if close and close[0] < 0.90 and close[1] < 0.12:
                self._tap_forced(*close)
            else:
                self.adb.key(4)
            CONTROL.sleep(0.45)
        return True

    def _dismiss_blocking_menu_if_no_camps(self, image: Any | None = None) -> bool:
        """Close hire/mail overlays when the map has grass but no camps. Never hire, never shop."""
        try:
            shot = image if image is not None else self._image()
        except Exception:
            return False
        if self._plan_or_picker_open(shot):
            return False
        if find_samurai_candidates(shot):
            return False
        if self._dismiss_inbox_if_open(shot):
            return True
        close = find_parchment_title_close(shot)
        if close and close[0] < 0.90 and close[1] < 0.12:
            logger.info(
                "Меню поверх карты без лагерей — закрываю крестик ({:.3f}, {:.3f})",
                close[0],
                close[1],
            )
            self._tap_forced(*close)
            CONTROL.sleep(0.4)
            return True
        return False

    def _dismiss_taxes_if_open(self, image: Any | None = None) -> bool:
        """Close «Налоги» with the parchment X. Never bribe/+20% and never shop."""
        try:
            shot = image if image is not None else self._image()
        except Exception:
            return False
        if self._plan_or_picker_open(shot):
            return False
        if not is_taxes_dialog(shot):
            return False
        close = find_parchment_title_close(shot) or (0.823, 0.038)
        if close[1] > 0.12 or close[0] >= 0.90:
            close = (0.823, 0.038)
        logger.info("Закрываю налоги только крестиком ({:.3f}, {:.3f})", close[0], close[1])
        self._tap_forced(*close)
        CONTROL.sleep(0.4)
        return True

    def _dismiss_blocking_overlay(self) -> bool:
        """Close a recognized blocker. Never tap offer-rail shop/chest buttons."""
        if self._dismiss_connection_error_if_open():
            return True
        if self._dismiss_special_offers_if_open():
            return True
        if self._dismiss_reward_popups():
            return True
        if self._dismiss_hire_menu_if_open():
            return True
        if self._dismiss_inbox_if_open():
            return True
        if self._dismiss_taxes_if_open():
            return True
        try:
            image = self._image()
        except Exception:
            return False
        if self._plan_or_picker_open(image):
            return False
        action = popup_action(image)
        if action:
            self._tap_norm(*action)
            return True
        return False

    def _tap_norm(self, nx: float, ny: float) -> None:
        if not self._rail_guard_allows(nx, ny):
            return
        size = self._size()
        x, y = _abs_point(size, [nx, ny])
        jx, jy = tap_jitter(5)
        self.adb.tap(x + jx, y + jy, source_size=size)
        time.sleep(random.uniform(0.25, 0.7))

    def _tap_norm_exact(self, nx: float, ny: float) -> None:
        if not self._rail_guard_allows(nx, ny):
            return
        size = self._size()
        x, y = _abs_point(size, [nx, ny])
        self.adb.tap(x, y, source_size=size)
        time.sleep(0.5)

    def _tap_forced(self, nx: float, ny: float) -> None:
        """Click a known in-plan control (formation X). Skips shop-rail guard."""
        size = self._size()
        x, y = _abs_point(size, [nx, ny])
        self.adb.tap(x, y, source_size=size)
        time.sleep(0.5)

    def close_formation_plan(self) -> bool:
        """Leave attack planning via the parchment X — used when a preset must not store units."""
        image = self._image()
        if not is_formation_screen(image):
            return False
        point = find_red_cross_force(image, title_bar_only=True, allow_right_chrome=True)
        if point is None:
            close = (self.layout.get("buttons") or {}).get("formation_close") or [0.94, 0.034]
            point = (float(close[0]), float(close[1]))
        logger.warning("Закрываю план атаки крестиком ({:.3f}, {:.3f})", point[0], point[1])
        self._tap_forced(*point)
        CONTROL.sleep(0.6)
        return True

    @staticmethod
    def _positive_unit_fill(ratio: tuple[int, int] | None) -> bool:
        return bool(ratio and ratio[0] > 0 and ratio[1] > 0)

    def _picker_overlay_open(self, image: Any) -> bool:
        """True when the unit picker overlay is visible. Never the empty-wave warning."""
        if find_picker_cards(image):
            return True
        if find_picker_confirm_button(image) is not None:
            return True
        if find_picker_max_control(image) is not None:
            return True
        return False

    def _dismiss_empty_wave_warning(self) -> bool:
        """Close «не назначив солдат» with its mid-dialog green check."""
        try:
            image = self._image()
        except Exception:
            return False
        if is_no_commanders_parchment(image):
            return False
        point = find_empty_wave_warning_confirm(image)
        if point is None:
            return False
        logger.warning(
            "Закрываю предупреждение пустой волны ({:.3f}, {:.3f}) — солдат в волне нет",
            point[0],
            point[1],
        )
        self._tap_norm_exact(*point)
        CONTROL.sleep(0.45)
        return True

    def _dismiss_no_commanders(self, image: Any | None = None) -> bool:
        """If the hire-reserve parchment is open, read it and close with red X only."""
        try:
            shot = image if image is not None else self._image()
        except Exception:
            return False
        diagnostic = no_commanders_diagnostics(shot)
        if not diagnostic["valid"] or diagnostic["point"] is None:
            return False
        save_shot(shot, "no-commanders.png")
        logger.warning(
            "Надпись: {} → {}",
            (diagnostic.get("text") or "").replace("\n", " ")[:180],
            diagnostic.get("conclusion") or "нет свободных военачальников",
        )
        point = diagnostic["point"]
        if is_green_hire_point(*point):
            logger.error("Отказ: зелёная печать найма за рубины — не жму")
            return False
        if is_offer_rail_point(*point):
            alt = [
                (nx, ny)
                for _score, nx, ny in _no_commanders_red_closes(shot)
                if not is_offer_rail_point(nx, ny) and not is_green_hire_point(nx, ny)
            ]
            if alt:
                point = alt[0]
        logger.warning(
            "Закрываю табличку наместников/военачальников красным крестиком ({:.3f}, {:.3f})",
            point[0],
            point[1],
        )
        self._tap_norm_exact(*point)
        CONTROL.sleep(0.5)
        self._no_commanders_seen = True
        return True

    def _open_unit_picker(self, slot_key: str) -> tuple[Any | None, str]:
        """Tap the flank cell unless the picker is already open."""
        image = self._image()
        if self._picker_overlay_open(image):
            logger.info("Пикер уже открыт — жму MAX и галочку без повторного закрытия")
            return image, ""
        self.tap_rel(slot_key)
        picker = self._wait_for(
            self._picker_overlay_open,
            timeout=self._vision_seconds("picker_open_timeout_seconds", 8),
            label="открытие пикера",
        )
        if picker is None:
            return None, "unit_picker_not_found"
        return picker, ""

    def _picker_confirm_point(self, image: Any) -> tuple[float, float] | None:
        point = find_picker_confirm_button(image)
        if point is not None:
            return point
        if not self._picker_overlay_open(image):
            return None
        layout_point = (getattr(self, "layout", None) or {}).get("buttons", {}).get(
            "unit_picker_confirm"
        )
        if layout_point:
            return float(layout_point[0]), float(layout_point[1])
        return None

    def diagnose_unit_picker_confirm(
        self,
        click: bool = False,
        observed_fill: tuple[int, int] | None = None,
    ) -> tuple[bool, str, Any | None]:
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
        if (not diagnostic["valid"] or point is None) and self._positive_unit_fill(
            observed_fill
        ):
            fallback = self._picker_confirm_point(image)
            if fallback is not None:
                point = fallback
                diagnostic = {**diagnostic, "point": point, "valid": True}
                logger.warning(
                    "OCR/шаблон галочки слабый — жму по разметке ({:.3f}, {:.3f})",
                    point[0],
                    point[1],
                )
        if not diagnostic["valid"] or point is None:
            return False, "unit_picker_confirm_not_confident", None
        if not click:
            return True, "diagnostic_only", image
        picker_units = self._read_ratio_from_image(image, "picker_units")
        filled = self._positive_unit_fill(observed_fill) or self._positive_unit_fill(
            picker_units
        )
        # Empty confirm is the only abort: picker still 0/N. A later frame after
        # the overlay closes is not a fill failure — that is the success path.
        if not filled:
            save_shot(image, "unit-picker-confirm-fill-not-retained.png")
            return False, "unit_picker_fill_not_retained", None
        logger.info(
            "Пикер: жму галочку ({:.3f}, {:.3f}) после заполнения {}",
            point[0],
            point[1],
            observed_fill or picker_units,
        )
        self._tap_norm_exact(*point)
        after = self._wait_for(
            self._is_plain_formation,
            timeout=self._vision_seconds("picker_confirm_timeout_seconds", 8),
            label="закрытие пикера",
        )
        latest = after if after is not None else self._image()
        if is_map_screen(latest):
            save_shot(latest, "unit-picker-confirm-transition-failed.png")
            return False, "unit_picker_confirm_transition_failed", None
        if after is not None:
            save_shot(after, "unit-picker-confirm-after.png")
            return True, "confirmed", after
        picker_gone = find_picker_confirm_button(latest) is None and not find_picker_cards(
            latest
        )
        if picker_gone:
            save_shot(latest, "unit-picker-confirm-after.png")
            return True, "confirmed", latest
        leftover = self._read_ratio_from_image(latest, "picker_units")
        if leftover is not None and leftover[0] <= 0:
            if self._positive_unit_fill(observed_fill):
                logger.info(
                    "После галочки OCR дал 0/N при уже заполненном {}/{} — план не закрываю, иду в Нападение",
                    observed_fill[0],
                    observed_fill[1],
                )
                save_shot(latest, "unit-picker-confirm-after.png")
                return True, "confirmed", latest
            save_shot(latest, "unit-picker-confirm-fill-not-retained.png")
            return False, "unit_picker_fill_not_retained", None
        save_shot(latest, "unit-picker-confirm-transition-failed.png")
        return False, "unit_picker_confirm_transition_failed", None

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
        if self._dismiss_no_commanders(image):
            return False, "no_commanders", None
        if not diagnostic["valid"] or point is None:
            return False, "movement_confirm_not_confident", None
        if not click:
            return True, "diagnostic_only", image
        self._tap_norm_exact(*point)
        after = self._wait_for(is_map_screen, timeout=8)
        latest = after if after is not None else self._image()
        if self._dismiss_no_commanders(latest):
            return False, "no_commanders", None
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
            source_size=(width, height),
        )
        CONTROL.sleep(0.6)

    def scroll_tool_inventory(self) -> None:
        """One small inventory step, then caller screenshots. Never shop/search."""
        size = self._size()
        cx, cy = 0.40, 0.56
        x, y = _abs_point(size, [cx, cy])
        logger.info("Мелкий скролл орудий ({:.3f}, {:.3f})", cx, cy)
        self.adb.wheel(x, y, delta=-120, source_size=size)
        width, height = size
        self.adb.swipe(
            round(0.40 * width),
            round(0.57 * height),
            round(0.40 * width),
            round(0.61 * height),
            duration_ms=160,
            source_size=size,
        )
        CONTROL.sleep(0.22)

    def _pan_map(self, view_dx: float, view_dy: float) -> None:
        """Move the kingdom-map view. Positive dx looks east; the drag is inverted."""
        start = (0.50, 0.52)
        finish = (
            min(0.78, max(0.18, 0.50 - float(view_dx))),
            min(0.82, max(0.20, 0.52 - float(view_dy))),
        )
        self._swipe_norm(start, finish)

    def _read_map_coords(self, image: Any) -> tuple[tuple[int, int] | None, tuple[int, int] | None]:
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
        viewport = (
            (viewport_x, viewport_y)
            if viewport_x is not None and viewport_y is not None
            else None
        )
        return main, viewport

    @staticmethod
    def _coords_plausible(
        main: tuple[int, int] | None,
        viewport: tuple[int, int] | None,
        radius: int = 80,
    ) -> bool:
        if main is None or viewport is None:
            return False
        return abs(main[0] - viewport[0]) <= radius and abs(main[1] - viewport[1]) <= radius

    def _map_scan_offsets(self) -> list[tuple[float, float]]:
        vision = self.config.get("vision") or {}
        span = vision.get("map_scan_span") or [0.30, 0.24]
        sx = float(span[0])
        sy = float(span[1] if len(span) > 1 else span[0])
        rings = max(1, int(vision.get("map_scan_rings") or 2))
        offsets: list[tuple[float, float]] = []
        for ring in range(1, rings + 1):
            rx, ry = sx * ring, sy * ring
            offsets.extend(
                [
                    (rx, 0.0),
                    (0.0, ry),
                    (-rx, 0.0),
                    (0.0, -ry),
                    (rx, ry),
                    (-rx, ry),
                    (-rx, -ry),
                    (rx, -ry),
                ]
            )
        return offsets

    def _is_blocked_screen_target(self, point: tuple[float, float]) -> bool:
        for bx, by in self._blocked_screen_targets:
            if (point[0] - bx) ** 2 + (point[1] - by) ** 2 < 0.012**2:
                return True
        return False

    def _jump_to_coords(self, coords: tuple[int, int]) -> None:
        logger.warning(
            "Переход через поиск {}:{} отключён — кнопка search на рейке спецпредложений",
            coords[0],
            coords[1],
        )

    def _recenter_on_main_castle(self, image: Any) -> Any:
        """Keep the hunt anchored on the account's MAIN castle, not the last target."""
        main, viewport = self._read_map_coords(image)
        marker = find_main_castle_marker(image)
        if marker and 0.32 < marker[0] < 0.68 and 0.38 < marker[1] < 0.72:
            return image
        if main and viewport and self._coords_plausible(main, viewport):
            if abs(main[0] - viewport[0]) + abs(main[1] - viewport[1]) <= 4:
                return image
        if marker:
            logger.info("Центрирую главный замок на карте")
            self._pan_map(marker[0] - 0.50, marker[1] - 0.54)
            CONTROL.sleep(0.2)
            return self._image()
        if main:
            logger.info(
                "Не жму поиск к замку {}:{} — search на рейке спецпредложений",
                main[0],
                main[1],
            )
        return image

    def wait_out_loading(self, timeout: float = 180.0) -> bool:
        """True while the game is still connecting — never tap shop/search/rubies."""
        try:
            image = self._image()
        except Exception:
            return False
        # Popups over the map are not «loading» — clear them first.
        if self._dismiss_connection_error_if_open(image):
            image = self._image()
        if self._dismiss_special_offers_if_open(image):
            image = self._image()
        if self._dismiss_reward_popups(image):
            image = self._image()
        if self._dismiss_hire_menu_if_open(image):
            image = self._image()
        if not is_loading_screen(image) and not is_connection_error_dialog(image):
            return False
        logger.info("Игра грузит игровой сервер — жду карту, не жму магазин и поиск")
        deadline = time.time() + timeout
        while time.time() < deadline:
            CONTROL.sleep(2.0)
            try:
                image = self._image()
            except Exception:
                continue
            if self._dismiss_connection_error_if_open(image):
                continue
            if self._dismiss_special_offers_if_open(image):
                continue
            if self._dismiss_reward_popups(image):
                continue
            if is_map_screen(image) and not is_loading_screen(image):
                self._dismiss_reward_popups()
                self._dismiss_special_offers_if_open()
                logger.info("Карта мира появилась — продолжаю")
                return False
        logger.warning("Сервер всё ещё грузится — повторю цикл без кликов")
        return True

    def _hunt_quota(self) -> int:
        """How many unique targets to collect before attacking."""
        kind = str((self.config or {}).get("current_target_kind") or "")
        if kind == "samurai":
            return 4
        cfg = self.config or {}
        for key in ("max_commanders", "commanders", "army_slots", "max_waves"):
            value = cfg.get(key)
            if value not in (None, "", 0):
                return max(1, min(30, int(value)))
        return DEFAULT_HUNT_BATCH

    def _await_world_map(self, timeout: float = 6.0) -> Any | None:
        """Wait for the kingdom map without ESC/formation_close after a successful plan."""
        image = self._image()
        if is_map_screen(image) and find_samurai_candidates(image):
            return image
        if self._dismiss_inbox_if_open(image):
            CONTROL.sleep(0.3)
            image = self._image()
            if is_map_screen(image) and find_samurai_candidates(image):
                return image
        if self._dismiss_blocking_menu_if_no_camps(image):
            CONTROL.sleep(0.3)
            image = self._image()
            if is_map_screen(image) and find_samurai_candidates(image):
                return image
        if is_map_screen(image):
            if self._dismiss_taxes_if_open(image) or self._dismiss_special_offers_if_open(image):
                CONTROL.sleep(0.35)
                image = self._image()
            if is_map_screen(image) and find_samurai_candidates(image):
                return image
            if is_map_screen(image) and not is_taxes_dialog(image):
                return image
        logger.info("Жду карту без закрытия плана крестиком")
        return self._wait_for(
            lambda img: is_map_screen(img)
            and not is_taxes_dialog(img)
            and not is_special_offers_screen(img),
            timeout=timeout,
        )

    def _list_eligible_targets(self, image: Any, kind: str) -> list[HuntTarget]:
        if kind == "samurai":
            threshold = float((self.config.get("vision") or {}).get("samurai_threshold") or 0.65)
            candidates = find_samurai_candidates(image, threshold)
            skip_burning = True
        else:
            threshold = float((self.config.get("vision") or {}).get("robber_threshold") or 0.65)
            candidates = find_robber_candidates(image, threshold)
            skip_burning = False
        if not candidates:
            return []
        main, viewport = self._read_map_coords(image)
        if main is None or viewport is None or not self._coords_plausible(main, viewport):
            logger.warning(
                "Не прочитаны координаты главного замка или карты — беру видимые цели по карте"
            )
            eligible: list[tuple[float, float, float]] = []
            for candidate in candidates:
                point = (candidate[0], candidate[1])
                if not skip_burning and is_burning_candidate(image, point):
                    continue
                if is_offer_rail_point(point[0], point[1]):
                    continue
                if self._is_blocked_screen_target(point):
                    continue
                eligible.append(candidate)
            if not eligible:
                return []
            marker = find_main_castle_marker(image)
            anchor = marker or (0.50, 0.54)
            eligible.sort(
                key=lambda item: (item[0] - anchor[0]) ** 2 + (item[1] - anchor[1]) ** 2
            )
            return [HuntTarget((item[0], item[1]), None) for item in eligible]
        vision = self.config.get("vision") or {}
        anchor_raw = vision.get("map_anchor") or [0.50, 0.54]
        scale_raw = vision.get("map_coordinate_scale") or [0.044, 0.044]
        kingdom = int((self.config.get("baron_attacks") or {}).get("kingdom", 0))
        found: list[HuntTarget] = []
        burning = 0
        cooling = 0
        blocked = 0
        for candidate in candidates:
            point = (candidate[0], candidate[1])
            if not skip_burning and is_burning_candidate(image, point):
                burning += 1
                continue
            if is_offer_rail_point(point[0], point[1]):
                continue
            if self._is_blocked_screen_target(point):
                blocked += 1
                continue
            coords = project_map_coordinate(
                point,
                viewport,
                (float(anchor_raw[0]), float(anchor_raw[1])),
                (float(scale_raw[0]), float(scale_raw[1])),
            )
            stable = (round(coords[0]), round(coords[1]))
            if not self.store.target_available(kind, kingdom, stable[0], stable[1]):
                cooling += 1
                continue
            found.append(HuntTarget(point, stable))
        main_marker = find_main_castle_marker(image)
        if main_marker:
            found.sort(
                key=lambda item: (item.point[0] - main_marker[0]) ** 2
                + (item.point[1] - main_marker[1]) ** 2
            )
        else:
            found.sort(
                key=lambda item: (
                    (item.coords[0] - main[0]) ** 2 + (item.coords[1] - main[1]) ** 2
                    if item.coords is not None
                    else 10**9
                )
            )
        if not found:
            logger.info(
                "Нет доступной цели на экране: найдено {}, горят {}, "
                "на перезарядке {}, заблокировано {}",
                len(candidates),
                burning,
                cooling,
                blocked,
            )
        return found

    def _collect_hunt_batch(self, kind: str) -> list[HuntTarget]:
        """Scan around the MAIN castle until N unique robbers are stored, then stop."""
        quota = self._hunt_quota()
        logger.info(
            "Охота: сначала набираю до {} целей, потом атаки",
            quota,
        )
        self.store.live.mode = "search"
        self.store.save()
        found: list[HuntTarget] = []
        seen: set[tuple[Any, ...]] = set()

        def ingest(image: Any) -> bool:
            if not is_map_screen(image):
                logger.info("Скан карты остановлен — экран больше не карта")
                return True
            for target in self._list_eligible_targets(image, kind):
                ident = target.identity()
                if ident in seen:
                    continue
                seen.add(ident)
                found.append(target)
                logger.info(
                    "В пачку охоты: {} ({}/{})",
                    target.coords or target.point,
                    len(found),
                    quota,
                )
                if len(found) >= quota:
                    return True
            return False

        image = self._image()
        if not (is_map_screen(image) and find_reward_confirm(image) is None):
            if self._dismiss_reward_popups(image) or self._dismiss_inbox_if_open(image) or self._dismiss_blocking_menu_if_no_camps(image) or self._dismiss_taxes_if_open(image) or self._dismiss_special_offers_if_open(image):
                CONTROL.sleep(0.45)
                image = self._image()
        image = self._recenter_on_main_castle(image)
        if not is_map_screen(image) and self._dismiss_blocking_overlay():
            CONTROL.sleep(0.4)
            image = self._recenter_on_main_castle(self._image())
        if ingest(image):
            logger.info("Пачка охоты готова: {} целей, полный скан больше не нужен", len(found))
            return found
        if found:
            logger.info(
                "На экране у замка уже {} целей — бью их до скана остальной карты",
                len(found),
            )
            return found
        for dx, dy in self._map_scan_offsets():
            logger.info("Скан карты: сдвиг ({:+.2f}, {:+.2f})", dx, dy)
            self._pan_map(dx, dy)
            CONTROL.sleep(0.25)
            image = self._image()
            stop = ingest(image)
            self._pan_map(-dx, -dy)
            CONTROL.sleep(0.15)
            if stop:
                break
        logger.info("Пачка охоты готова: {} целей", len(found))
        return found

    def _match_visible_target(
        self,
        image: Any,
        kind: str,
        target: HuntTarget,
    ) -> tuple[float, float] | None:
        for item in self._list_eligible_targets(image, kind):
            if is_burning_candidate(image, item.point):
                continue
            if target.coords and item.coords:
                if (
                    abs(item.coords[0] - target.coords[0]) <= 2
                    and abs(item.coords[1] - target.coords[1]) <= 2
                ):
                    return item.point
            dist2 = (item.point[0] - target.point[0]) ** 2 + (
                item.point[1] - target.point[1]
            ) ** 2
            if dist2 < 0.04**2:
                return item.point
        if kind == "samurai":
            nearest: tuple[float, HuntTarget] | None = None
            for item in self._list_eligible_targets(image, kind):
                if is_burning_candidate(image, item.point):
                    continue
                dist2 = (item.point[0] - target.point[0]) ** 2 + (
                    item.point[1] - target.point[1]
                ) ** 2
                if nearest is None or dist2 < nearest[0]:
                    nearest = (dist2, item)
            if nearest is not None:
                logger.info(
                    "Самурайский лагерь на экране ({:.3f}, {:.3f}), dist={:.3f}",
                    nearest[1].point[0],
                    nearest[1].point[1],
                    nearest[0] ** 0.5,
                )
                if nearest[1].coords:
                    self._selected_target_coords = nearest[1].coords
                return nearest[1].point
        return None

    def _focus_hunt_target(self, kind: str, target: HuntTarget) -> tuple[float, float] | None:
        """Open the next stored robber without a full map rescan."""
        image = self._await_world_map()
        if image is None:
            return None
        if self._dismiss_special_offers_if_open(image):
            image = self._await_world_map(timeout=4) or self._image()
        self._selected_target_coords = target.coords
        visible = self._match_visible_target(image, kind, target)
        if visible:
            if not self._selected_target_coords:
                self._selected_target_coords = target.coords
            return visible
        if kind == "samurai":
            camps = find_samurai_candidates(image)
            if camps:
                point = (float(camps[0][0]), float(camps[0][1]))
                logger.info(
                    "Беру видимый лагерь без сверки координат ({:.3f}, {:.3f})",
                    point[0],
                    point[1],
                )
                return point
            logger.info("Видимых лагерей нет — не панорамирую и не открываю поиск")
        else:
            recentered = self._recenter_on_main_castle(image)
            visible = self._match_visible_target(recentered, kind, target)
            if visible:
                self._selected_target_coords = target.coords
                return visible
        logger.info(
            "Цель {} не на экране — поиск/магазин не жму, пропускаю",
            target.coords or target.point,
        )
        return None

    def _hunt_robbers(self, kind: str) -> tuple[float, float] | None:
        """Swipe around the MAIN castle until an eligible robber is on screen."""
        logger.info("Ищу замки разбойников вокруг главного замка")
        self.store.live.mode = "search"
        self.store.save()
        image = self._recenter_on_main_castle(self._image())
        if not is_map_screen(image):
            logger.info("Скан карты остановлен — экран больше не карта")
            return None
        point = self._select_visible_target(image, kind)
        if point:
            return point
        for dx, dy in self._map_scan_offsets():
            logger.info("Скан карты: сдвиг ({:+.2f}, {:+.2f})", dx, dy)
            self._pan_map(dx, dy)
            CONTROL.sleep(0.25)
            image = self._image()
            if not is_map_screen(image):
                logger.info("Скан карты остановлен — экран больше не карта")
                return None
            point = self._select_visible_target(image, kind)
            if point:
                return point
            self._pan_map(-dx, -dy)
            CONTROL.sleep(0.15)
        logger.info("Скан карты: вокруг главного замка свободных разбойников нет")
        return None

    def dismiss_popups(self) -> None:
        if self._dismiss_blocking_overlay():
            return
        extra = self.layout.get("dismiss") or []
        for point in extra:
            self._tap_norm(float(point[0]), float(point[1]))

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

    def _wait_for(
        self,
        predicate: Any,
        timeout: float | None = None,
        *,
        label: str = "",
        heartbeat: float = 2.0,
    ) -> Any | None:
        timeout = float(
            timeout
            or (self.config.get("vision") or {}).get("screen_timeout_seconds")
            or 8
        )
        deadline = time.time() + timeout
        started = time.time()
        next_heartbeat = started + max(0.5, float(heartbeat))
        while time.time() < deadline:
            CONTROL.check()
            image = self._image()
            if predicate(image):
                return image
            if label and time.time() >= next_heartbeat:
                logger.info("Пикер: жду {}… ({:.0f}s)", label, time.time() - started)
                next_heartbeat = time.time() + max(0.5, float(heartbeat))
            CONTROL.sleep(0.35)
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
            elif is_formation_screen(image) or find_picker_cards(image) or find_picker_confirm_button(image):
                logger.warning(
                    "ensure_map: план/picker открыт — не жму крестик, карту, ESC и не закрываю"
                )
                return image
            else:
                action = popup_action(image)
                if action:
                    self._tap_norm(*action)
                else:
                    self.tap_rel("map")
            time.sleep(0.9)
        image = self._image()
        return image if is_map_screen(image) else None

    def _choose_visible_target_without_ocr(
        self,
        image: Any,
        kind: str,
        candidates: list[tuple[float, float, float]],
    ) -> tuple[float, float] | None:
        del kind
        eligible: list[tuple[float, float, float]] = []
        for candidate in candidates:
            point = (candidate[0], candidate[1])
            if is_burning_candidate(image, point):
                continue
            if is_offer_rail_point(point[0], point[1]):
                continue
            if self._is_blocked_screen_target(point):
                continue
            eligible.append(candidate)
        if not eligible:
            return None
        marker = find_main_castle_marker(image)
        anchor = marker or (0.50, 0.54)
        chosen_candidate = min(
            eligible,
            key=lambda item: (item[0] - anchor[0]) ** 2 + (item[1] - anchor[1]) ** 2,
        )
        chosen = (chosen_candidate[0], chosen_candidate[1])
        self._selected_target_coords = (
            round(chosen[0] * 1000),
            round(chosen[1] * 1000),
        )
        logger.info(
            "Выбрана видимая цель без OCR: {}/{} кандидатов, точка {:.2f},{:.2f}",
            len(eligible),
            len(candidates),
            chosen[0],
            chosen[1],
        )
        return chosen

    def _select_visible_target(self, image: Any, kind: str) -> tuple[float, float] | None:
        targets = self._list_eligible_targets(image, kind)
        if not targets:
            return None
        chosen = targets[0]
        if chosen.coords is not None:
            self._selected_target_coords = chosen.coords
        else:
            self._selected_target_coords = (
                round(chosen.point[0] * 1000),
                round(chosen.point[1] * 1000),
            )
        logger.info(
            "Выбрана ближайшая к главному замку цель {} из {} видимых",
            chosen.coords or chosen.point,
            len(targets),
        )
        return chosen.point

    def _open_formation(self, point: tuple[float, float], kind: str) -> bool:
        already = self._image()
        if self._plan_or_picker_open(already):
            logger.info("Планирование уже открыто — не закрываю и не жму карту")
            return True
        self._tap_norm(*point)
        popup = self._wait_for(lambda image: find_target_attack_button(image) is not None, timeout=5)
        if popup is None:
            blocked = self._image()
            if self._plan_or_picker_open(blocked):
                return True
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
            logger.warning(
                "Координаты цели с таблички не прочитались — оставляю координаты охоты {}",
                self._selected_target_coords,
            )
        else:
            if (
                kind == "samurai"
                and self._selected_target_coords
                and abs(target_x - self._selected_target_coords[0])
                + abs(target_y - self._selected_target_coords[1])
                > 8
            ):
                logger.warning(
                    "Табличка дала ({}, {}) — оставляю охоту {}",
                    target_x,
                    target_y,
                    self._selected_target_coords,
                )
            else:
                self._selected_target_coords = (target_x, target_y)
            kingdom = int((self.config.get("baron_attacks") or {}).get("kingdom", 0))
            plaque = self._selected_target_coords or (target_x, target_y)
            if not self.store.target_available(kind, kingdom, plaque[0], plaque[1]):
                logger.info(f"Цель {plaque[0]}:{plaque[1]} ещё на локальной перезарядке")
                self.tap_rel("map")
                return False
        if kind == "samurai":
            title = ocr_text_ui(crop_rel(popup, [0.18, 0.14, 0.82, 0.32]), psm=6)
            body = ocr_text_ui(crop_rel(popup, [0.18, 0.20, 0.82, 0.55]), psm=6)
            blob = f"{title} {body}"
            level = parse_samurai_camp_level(blob)
            logger.info("OCR уровня лагеря: {} / {}", level, blob[:80])
            try:
                coords = self._selected_target_coords or (0, 0)
                save_shot(popup, f"samurai_level_{coords[0]}_{coords[1]}.png")
            except Exception:
                pass
            if level is not None and self._selected_target_coords:
                remaining = remaining_attacks_from_level(level)
                self.store.set_samurai_remaining(self._selected_target_coords, remaining)
                if remaining is not None and remaining <= 0:
                    logger.info("Лагерь {} без атак — закрываю табличку", self._selected_target_coords)
                    self.tap_rel("map")
                    return False
        attack_point = find_target_attack_button(popup)
        if attack_point is None:
            return False
        self._tap_norm(*attack_point)
        time.sleep(0.8)
        if kind == "samurai":
            opened = self._wait_for(
                lambda img: is_difficulty_dialog(img)
                or is_formation_screen(img)
                or is_no_commanders_parchment(img),
                timeout=6,
                label="сложность или план самураев",
            )
            shot = opened if opened is not None else self._image()
            if self._dismiss_no_commanders(shot):
                return False
            if is_difficulty_dialog(shot):
                logger.info("Окно «выберите сложность» — не жду план, отдам модулю самураев")
                return True
            if is_formation_screen(shot):
                return True
        self.tap_rel("start_attack_confirm")
        opened = self._wait_for(
            lambda img: is_formation_screen(img)
            or is_no_commanders_parchment(img)
            or (kind == "samurai" and is_difficulty_dialog(img)),
            timeout=10,
            label="формирование",
        )
        shot = opened if opened is not None else self._image()
        if self._dismiss_no_commanders(shot):
            return False
        if kind == "samurai" and is_difficulty_dialog(shot):
            return True
        return opened is not None and is_formation_screen(self._image())

    def _read_ratio(self, key: str) -> tuple[int, int] | None:
        return parse_ratio(self.read_region(key))

    def _is_plain_formation(self, image: Any) -> bool:
        return is_formation_screen(image) and not find_picker_cards(image)

    def _select_best_picker_card(self) -> bool:
        """Select one detected card with strict progress and time bounds."""
        timeout = self._vision_seconds("picker_timeout_seconds", 15)
        deadline = time.time() + timeout
        CONTROL.check()
        image = self._image()
        before = self._read_ratio_from_image(image, "picker_units")
        if not before or time.time() >= deadline:
            return False
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
            CONTROL.sleep(0.3)
            after_image = self._image()
            after = self._read_ratio_from_image(after_image, "picker_units")
        logger.info(f"Выбор юнитов в ячейке: {before} -> {after}")
        if after and after[0] > 0:
            self._last_picker_fill = after
        if after is None and self._is_plain_formation(after_image):
            if before[1] > 0:
                self._last_picker_fill = (before[1], before[1])
            CONTROL.sleep(1.0)
            return True
        if after is None and find_picker_confirm_button(after_image) is not None:
            return True
        return bool(after and (after[0] > before[0] or after[0] >= after[1] > 0))

    def _assume_picker_capacity(self, image: Any, before: tuple[int, int] | None) -> tuple[int, int]:
        ratio = self._read_ratio_from_image(image, "picker_units")
        cap = ratio[1] if ratio and ratio[1] > 0 else 0
        if cap <= 0 and before and before[1] > 0:
            cap = before[1]
        if cap <= 0 and self._last_picker_fill and self._last_picker_fill[1] > 0:
            cap = self._last_picker_fill[1]
        if cap <= 0:
            cap = 10
        cur = ratio[0] if ratio and ratio[0] > 0 else 0
        if before and before[0] > cur:
            cur = before[0]
        if self._last_picker_fill and self._last_picker_fill[0] > cur:
            cur = self._last_picker_fill[0]
        return cur, cap

    def _vision_fill_picker_fallback(self) -> bool:
        """When OCR is silent but the picker overlay is visible, still MAX and proceed."""
        image = self._image()
        if not self._picker_overlay_open(image):
            return False
        logger.warning("Пикер: OCR застрял — MAX по шаблону/разметке без ожидания 10/10")
        point = find_picker_max_control(image)
        if point is not None:
            self._tap_norm(*point)
        else:
            layout = (getattr(self, "layout", None) or {}).get("buttons", {}).get("picker_max")
            if layout:
                self._tap_norm_exact(float(layout[0]), float(layout[1]))
        CONTROL.sleep(0.5)
        cur, cap = self._assume_picker_capacity(self._image(), self._last_picker_fill)
        if cur <= 0:
            logger.warning("Пикер: fallback MAX не дал заполнения")
            return False
        self._last_picker_fill = (cur, cap)
        logger.info("Пикер: overlay виден — считаю {}/{} и иду к галочке", cur, cap)
        return True

    def _dump_picker_max(self) -> bool:
        """LEFT-side MAX that fills to capacity. Never the cancel that zeros."""
        wait_seconds = self._vision_seconds("picker_max_wait_seconds", 10)
        image = self._image()
        before = self._read_ratio_from_image(image, "picker_units")
        point = find_picker_max_control(image)
        if point is not None:
            logger.info("Жму MAX пикера по шаблону ({:.3f}, {:.3f})", point[0], point[1])
            self._tap_norm(*point)
        else:
            logger.info("Жму MAX пикера по разметке picker_max (левый MAX, не ноль)")
            layout = (getattr(self, "layout", None) or {}).get("buttons", {}).get("picker_max")
            if layout:
                self._tap_norm_exact(float(layout[0]), float(layout[1]))
            else:
                self.tap_rel("picker_max")
        deadline = time.time() + wait_seconds
        started = time.time()
        next_heartbeat = started + 2.0
        latest: tuple[int, int] | None = before if self._positive_unit_fill(before) else None
        while time.time() < deadline:
            CONTROL.check()
            shot = self._image()
            ratio = self._read_ratio_from_image(shot, "picker_units")
            if ratio and ratio[1] > 0:
                if self._positive_unit_fill(ratio):
                    latest = ratio
                if ratio[0] >= ratio[1]:
                    progressed = (
                        before is None
                        or ratio[0] > before[0]
                        or ratio[1] != before[1]
                    )
                    if progressed:
                        self._last_picker_fill = ratio
                        logger.info("Пикер после MAX: {}/{}", ratio[0], ratio[1])
                        return True
            if self._picker_confirm_point(shot) is not None:
                cur, cap = self._assume_picker_capacity(shot, before)
                if cur > 0:
                    self._last_picker_fill = (cur, cap)
                    logger.info(
                        "Пикер: галочка видна после MAX — считаю {}/{}",
                        cur,
                        cap,
                    )
                    return True
            if time.time() >= next_heartbeat:
                logger.info("Пикер: жду MAX… ({:.0f}s)", time.time() - started)
                next_heartbeat = time.time() + 2.0
            CONTROL.sleep(0.25)
        if latest:
            self._last_picker_fill = latest
        return bool(latest and latest[0] >= latest[1] > 0)

    def _fill_picker_to_capacity(self) -> bool:
        """Select a unit row if empty, then MAX until current==capacity."""
        if not self._positive_unit_fill(self._last_picker_fill):
            self._select_best_picker_card()
        max_attempts = int((self.config.get("vision") or {}).get("picker_max_attempts") or 3)
        wait_seconds = self._vision_seconds("picker_fill_timeout_seconds", 10)
        for attempt in range(max(1, max_attempts)):
            if self._dump_picker_max():
                return True
            if not self._positive_unit_fill(self._last_picker_fill):
                self._select_best_picker_card()
            if attempt + 1 < max_attempts:
                logger.info("MAX без заполнения — повтор {}/{}", attempt + 2, max_attempts)
                CONTROL.sleep(0.35)
        if self._vision_fill_picker_fallback():
            return True
        if not self._select_best_picker_card():
            return self._vision_fill_picker_fallback()
        deadline = time.time() + wait_seconds
        started = time.time()
        next_heartbeat = started + 2.0
        while time.time() < deadline:
            CONTROL.check()
            shot = self._image()
            ratio = self._read_ratio_from_image(shot, "picker_units")
            if ratio and ratio[1] > 0 and ratio[0] >= ratio[1]:
                self._last_picker_fill = ratio
                logger.info("Пикер заполнен {}/{}", ratio[0], ratio[1])
                return True
            if self._positive_unit_fill(ratio):
                self._last_picker_fill = ratio
                if self._picker_confirm_point(shot) is not None:
                    logger.info("Пикер: есть солдаты {}/{} и галочка", ratio[0], ratio[1])
                    return True
            if time.time() >= next_heartbeat:
                logger.info("Пикер: жду 10/10… ({:.0f}s)", time.time() - started)
                next_heartbeat = time.time() + 2.0
            CONTROL.sleep(0.25)
        filled = self._last_picker_fill
        if filled and filled[1] > 0 and filled[0] >= filled[1]:
            return True
        return self._vision_fill_picker_fallback()

    def _prepare_single_center_wave(self) -> tuple[bool, str]:
        # Every attack, including the 4th+: cell → MAX → 10/10 → green check.
        # Do not skip confirm because leftover OCR still looks like 10/10.
        last_reason = ""
        for sequence_attempt in range(2):
            self._dismiss_empty_wave_warning()
            if sequence_attempt:
                logger.warning("Пикер: повтор полной последовательности 2/2")
                slot = self.layout.get("buttons", {}).get("unit_slot")
                if slot:
                    self._tap_norm_exact(float(slot[0]), float(slot[1]))
                    CONTROL.sleep(0.5)
            self._last_picker_fill = None
            ok, reason = self._prepare_single_center_wave_once()
            if ok:
                return True, ""
            last_reason = reason
            if reason not in self._PICKER_STALL_REASONS:
                return False, reason
        return False, last_reason

    def _prepare_single_center_wave_once(self) -> tuple[bool, str]:
        formation = self._image()
        max_actions = int((self.config.get("vision") or {}).get("picker_max_actions") or 4)
        slot_keys = ("unit_slot", "unit_slot_second")[:max_actions]
        for slot_key in slot_keys:
            picker, reason = self._open_unit_picker(slot_key)
            if picker is None:
                return False, reason
            picker_ratio = self._read_ratio_from_image(picker, "picker_units")
            if (not picker_ratio or picker_ratio[1] <= 0) and self._picker_overlay_open(picker):
                picker_ratio = (0, 10)
            if not picker_ratio or picker_ratio[1] <= 0:
                return False, "center_capacity_not_read"
            if not self._fill_picker_to_capacity():
                save_shot(self._image(), "unit-picker-selection-no-progress.png")
                return False, "unit_picker_selection_no_progress"
            if not self._positive_unit_fill(self._last_picker_fill):
                return False, "unit_picker_fill_not_retained"
            confirmed, reason, formation = self.diagnose_unit_picker_confirm(
                click=True,
                observed_fill=self._last_picker_fill,
            )
            if not confirmed or formation is None:
                return False, reason
            units = self._read_ratio_from_image(formation, "formation_units")
            if units and units[0] == units[1] and units[1] > 0:
                break
            if units is None and self._positive_unit_fill(self._last_picker_fill):
                break
        observed = self._last_picker_fill
        final_units = self._read_ratio_from_image(formation, "formation_units")
        if final_units is not None and final_units[0] <= 0:
            save_shot(formation, "formation-units-empty.png")
            logger.warning(
                "Формирование {}/{} после пикера {} — Нападение не жму",
                final_units[0],
                final_units[1],
                observed,
            )
            return False, "formation_units_empty"
        if self._positive_unit_fill(observed) and not final_units:
            logger.info(
                "OCR формирования пуст после {}/{}; беру заполнение пикера",
                observed[0],
                observed[1],
            )
            final_units = observed
        final_tools = self._read_ratio_from_image(formation, "formation_tools")
        if final_tools is None and self._positive_unit_fill(observed):
            final_tools = (0, 0)
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

    def _escape_stuck_picker(self) -> None:
        """Force-close picker/formation after repeated stall so hunt can continue."""
        image = self._image()
        if find_picker_confirm_button(image) or find_picker_cards(image):
            cancel = (getattr(self, "layout", None) or {}).get("buttons", {}).get("picker_cancel")
            if cancel:
                logger.warning("Пикер: принудительно закрываю overlay")
                self._tap_norm_exact(float(cancel[0]), float(cancel[1]))
                CONTROL.sleep(0.6)
        image = self._image()
        if self._plan_or_picker_open(image):
            close = (getattr(self, "layout", None) or {}).get("buttons", {}).get("formation_close")
            if close and not is_offer_rail_point(float(close[0]), float(close[1])):
                logger.warning("Пикер: закрываю формирование — иду к следующей цели")
                self._tap_norm_exact(float(close[0]), float(close[1]))
                CONTROL.sleep(0.8)

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
        logger.warning(
            "Координатный поиск отключён — search ({:.2f},{:.2f}) открывает спецпредложения",
            0.90,
            0.14,
        )
        return "search_disabled"

    def on_screen_attack(self, kind: str) -> str:
        if getattr(self, "_hunt_queue", None) is None:
            self._hunt_queue = []
        if getattr(self, "_blocked_screen_targets", None) is None:
            self._blocked_screen_targets = []
        current = self._image()
        if self._dismiss_no_commanders(current):
            return "no_commanders"
        self._dismiss_empty_wave_warning()
        current = self._image()
        if self._plan_or_picker_open(current):
            logger.info(
                "Экран планирования уже открыт — не закрываю, продолжаю набор/Нападение"
            )
            return self._execute_formation_attack(kind, (0.50, 0.50))
        self._dismiss_special_offers_if_open(current)
        if not self._hunt_queue:
            self._blocked_screen_targets.clear()
            self._hunt_queue = self._collect_hunt_batch(kind)
            if not self._hunt_queue:
                logger.info("На карте нет доступных замков разбойников")
                return "no_targets"
            logger.info(
                "Охота закончена: {} целей, дальше бью по списку без повторного скана",
                len(self._hunt_queue),
            )
        while self._hunt_queue:
            current = self._image()
            if self._plan_or_picker_open(current):
                logger.info(
                    "Экран планирования уже открыт — не закрываю, продолжаю набор/Нападение"
                )
                return self._execute_formation_attack(kind, (0.50, 0.50))
            self._dismiss_special_offers_if_open(current)
            target = self._hunt_queue.pop(0)
            point = self._focus_hunt_target(kind, target)
            if point is None:
                logger.info(
                    "Цель {} пропала или горит — следующая из пачки без полного скана",
                    target.coords or target.point,
                )
                continue
            opened = False
            attempts = min(
                int((self.config.get("vision") or {}).get("popup_retries") or 4) + 1,
                3,
            )
            for _ in range(max(1, attempts)):
                if self._open_formation(point, kind):
                    opened = True
                    break
                if self._no_commanders_seen:
                    return "no_commanders"
                self._blocked_screen_targets.append(point)
                blocked = self._image()
                if self._plan_or_picker_open(blocked):
                    opened = True
                    break
                if self._dismiss_special_offers_if_open(blocked):
                    time.sleep(0.8)
                    point = self._focus_hunt_target(kind, target) or point
                    continue
                action = popup_action(blocked)
                if action:
                    self._tap_norm(*action)
                time.sleep(0.8)
                point = self._focus_hunt_target(kind, target) or point
            if not opened:
                logger.warning(
                    "Не открылся экран формирования — беру следующую цель из пачки"
                )
                continue
            return self._execute_formation_attack(kind, point)
        return "no_targets"

    def _tap_formation_attack(self) -> None:
        image = self._image()
        point = find_formation_attack_button(image)
        if point is not None:
            logger.info("Нападение: золотая кнопка ({:.3f}, {:.3f})", point[0], point[1])
            self._tap_norm(*point)
            return
        self.tap_rel("formation_attack")

    def _execute_formation_attack(self, kind: str, point: tuple[float, float]) -> str:
        self._dismiss_empty_wave_warning()
        self._last_picker_fill = None
        ok, reason = self._prepare_single_center_wave()
        if not ok:
            self.store.live.last_error = reason
            self.store.save()
            if reason in self._PICKER_STALL_REASONS:
                stall_count = getattr(self, "_picker_stall_count", 0) + 1
                self._picker_stall_count = stall_count
                max_stalls = int(
                    ((getattr(self, "config", None) or {}).get("vision") or {}).get(
                        "picker_stall_retries"
                    )
                    or 2
                )
                logger.warning(
                    "Формирование оставлено открытым для повтора: {} — крестик не жму ({}/{})",
                    reason,
                    stall_count,
                    max_stalls,
                )
                if stall_count >= max_stalls:
                    logger.warning(
                        "Пикер без прогресса {} раз — закрываю план и беру следующую цель",
                        stall_count,
                    )
                    self._escape_stuck_picker()
                    self._picker_stall_count = 0
            else:
                logger.warning(
                    "Формирование оставлено открытым для повтора: {} — крестик не жму",
                    reason,
                )
            return "unsafe_formation"
        self._picker_stall_count = 0
        self._tap_formation_attack()
        travel = self._wait_for(
            lambda img: is_travel_dialog(img) or is_no_commanders_parchment(img),
            timeout=self._vision_seconds("travel_dialog_timeout_seconds", 15),
            label="диалог похода",
        )
        if travel is not None and self._dismiss_no_commanders(travel):
            return "no_commanders"
        if travel is None:
            if self._dismiss_empty_wave_warning():
                logger.warning("Нападение отклонено: волна пустая")
                return "unsafe_formation"
            if self._dismiss_no_commanders():
                return "no_commanders"
            logger.warning("Нет диалога похода — план не закрываю крестиком")
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
            "x": int(
                self._selected_target_coords[0]
                if self._selected_target_coords
                else point[0] * 1000
            ),
            "y": int(
                self._selected_target_coords[1]
                if self._selected_target_coords
                else point[1] * 1000
            ),
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
            self._dismiss_blocking_overlay()
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
            self.telegram.report_status(
                f"🧪 DRY-RUN отменён перед отправкой: {kind} K{kid} ({x}, {y}), "
                f"время {one_way} сек"
            )
            return kind
        wait_for_send_slot(self.store, self.config)
        confirmed, reason, _ = self.diagnose_movement_confirm(click=True)
        if reason == "no_commanders":
            return "no_commanders"
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
        mark_successful_send(self.store, self.config)
        self.store.save()
        emit(
            "attack.sent",
            mode=self.store.live.active_mode or kind,
            kind=kind,
            kingdom=kid,
            x=x,
            y=y,
            one_way=one_way,
            movement=movement,
            commander=commander_no,
            screenshot=str(shot_path) if shot_path else "",
        )
        return kind

    def run_cycle(self) -> str:
        kind = str(self.config.get("current_target_kind") or "baron")
        emit(
            "attack.cycle.start",
            mode=self.store.live.active_mode or kind,
            kind=kind,
            style=str(self.config.get("attack_style") or "on_screen"),
            dry_run=bool(self.config.get("dry_run", True)),
        )
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
            emit("attack.cycle.end", result="wait_return", kind=kind)
            return "wait_return"
        result = self.on_screen_attack(kind)
        if result == "stop":
            emit("attack.cycle.end", result="stop", kind=kind, error=self.store.live.stopped_reason)
            return "stop"
        if result != kind:
            emit(
                "attack.cycle.end",
                result=result,
                kind=kind,
                error=self.store.live.last_error,
            )
            return result
        emit("attack.cycle.end", result="client:1", kind=kind)
        return f"client:1"
