from __future__ import annotations

import json
import random
import re
import time
from dataclasses import dataclass
from typing import Any

from loguru import logger
from PIL import ImageDraw, ImageStat

from e4kbot.bluestacks import AdbClient, capture_game_image, save_shot
from e4kbot.control import CONTROL
from e4kbot.farm_reports import (
    FarmLedger,
    WORLD_BY_MODE,
    find_messages_button,
    is_victory_report,
    parse_victory_report,
    unread_battle_rows,
    write_progress,
)
from e4kbot.paths import LAYOUTS_DIR, ROOT
from e4kbot.runtime.live import emit
from e4kbot.safety import (
    mark_successful_send,
    tap_jitter,
    wait_for_send_slot,
)
from e4kbot.state import StateStore
from e4kbot.telegram_bot import TelegramReporter
from e4kbot.world_switch import WorldSwitchMixin
from e4kbot.worlds import compact_ui, is_world_npc_kind, match_world_id, spec_for_kind, world_hunt_hits_are_cluster
from e4kbot.campaign_checkpoint import TOUR_MODE_IDS
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
    find_world_castle_candidates,
    find_samurai_candidates,
    find_nomad_candidates,
    find_formation_attack_button,
    find_formation_unit_slots,
    find_red_cross_force,
    find_target_attack_button,
    find_travel_seal_pair,
    find_plaque_attack_button,
    find_world_list_sextants,
    find_world_parchment_attack_button,
    find_parchment_title_close,
    _center_parchment_ratio,
    find_castle_name_hud_point,
    find_home_sextant_button,
    find_quit_dialog_no_button,
    is_castle_home_banner,
    flank_fill_allowed,
    is_connection_error_dialog,
    is_quit_game_dialog,
    is_formation_screen,
    is_choose_place_screen,
    is_world_list_open,
    is_burning_candidate,
    is_difficulty_dialog,
    is_event_reward_popup,
    is_green_hire_point,
    is_hire_menu,
    is_inbox_screen,
    is_info_plaque,
    is_loading_screen,
    is_map_screen,
    is_no_commanders_parchment,
    is_offer_rail_point,
    is_overview_plaque,
    is_ruby_plus_hud_point,
    is_ruby_shop,
    is_special_offers_screen,
    is_taxes_dialog,
    is_travel_dialog,
    map_grass_ratio,
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
    remaining_attacks_from_nomad_level,
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


def _dummy_center(point: tuple[float, float] | None) -> bool:
    if point is None:
        return True
    return abs(float(point[0]) - 0.50) < 0.02 and abs(float(point[1]) - 0.50) < 0.02


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


class BlueStacksEngine(WorldSwitchMixin):
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
        self._last_nomad_point: tuple[float, float] | None = None
        self._nomad_recenter_next = False
        self._picker_stall_count = 0
        self._no_commanders_seen = False
        self._world_scan = None
        self._switched_world_id: str | None = None
        self._need_ge_home = False
        self._world_recenter_tries = 0
        self._unopened_tries: dict[str, int] = {}
        self._report_check_queued = False
        self._last_report_check = 0.0
        self._farm_ledger = FarmLedger()
        self._cached_size: tuple[int, int] | None = None
        self._burst_send_times: list[float] = []
        self._speed_burst_done = False
        self._qualifying_cycles = 0
        self._tour_pack_ok: dict[str, bool] = {}
        self._pack_mode = None
        self._pack_prep_started_at: float | None = None
        self._pack_send_times: list[float] = []
        self._pack_finished = False
        try:
            from e4kbot.farm_reports import PROGRESS_PATH

            if PROGRESS_PATH.exists():
                payload = json.loads(PROGRESS_PATH.read_text(encoding="utf-8"))
                self._qualifying_cycles = int(payload.get("qualifying_cycles") or 0)
                self._tour_pack_ok = dict(payload.get("tour_pack_ok") or {})
        except Exception:
            self._qualifying_cycles = 0
        if self._qualifying_cycles < 10:
            sent_map = dict(getattr(self.store.live, "session_by_mode", None) or {})
            for mode_id in TOUR_MODE_IDS:
                sent_map[mode_id] = 0
            logger.warning(
                "SPEED: свежий тур — 10 циклов по 5×40с, сейчас {}/10",
                self._qualifying_cycles,
            )
            self.store.live.session_by_mode = sent_map
            self.store.live.post_attack_home_pending = False
            self.store.live.next_send_at = 0
            self.store.live.next_attack_at = 0
            self.store.save()

    def _speed_burst_active(self) -> bool:
        if getattr(self, "_burst_send_times", None) is None:
            return False
        return int(getattr(self, "_qualifying_cycles", 0) or 0) < 10

    def _begin_world_pack(self, kind: str) -> None:
        spec = spec_for_kind(kind)
        mode = spec.mode_id if spec is not None else kind
        if getattr(self, "_pack_mode", None) == mode and not getattr(self, "_pack_finished", False):
            return
        self._pack_mode = mode
        self._pack_prep_started_at = None
        self._pack_send_times = []
        self._pack_finished = False
        logger.warning(
            "PACK ARM {} — clock starts on first prep, cycles {}/10",
            mode,
            int(getattr(self, "_qualifying_cycles", 0) or 0),
        )

    def _mark_pack_prep(self, kind: str) -> None:
        if getattr(self, "_pack_finished", False):
            return
        if getattr(self, "_pack_prep_started_at", None) is not None:
            return
        if getattr(self, "_pack_mode", None) is None:
            self._begin_world_pack(kind)
        self._pack_prep_started_at = time.time()
        logger.warning(
            "PACK START {} prep clock — 5 sends ≤40s, cycles {}/10",
            getattr(self, "_pack_mode", kind),
            int(getattr(self, "_qualifying_cycles", 0) or 0),
        )

    def _note_pack_send(self, kind: str) -> None:
        now = time.time()
        if getattr(self, "_pack_prep_started_at", None) is None:
            self._mark_pack_prep(kind)
        times = getattr(self, "_pack_send_times", None)
        if times is None:
            self._pack_send_times = []
            times = self._pack_send_times
        times.append(now)
        spec = spec_for_kind(kind)
        mode = spec.mode_id if spec is not None else kind
        span = now - float(self._pack_prep_started_at or now)
        logger.warning(
            "PACK {} send {}/5 elapsed={:.2f}s (budget 40s from first prep)",
            mode,
            len(times),
            span,
        )
        if len(times) >= 5:
            self._close_world_pack(mode, span, span <= 40.0)

    def _close_world_pack(self, mode: str, span: float, ok: bool) -> None:
        packs = dict(getattr(self, "_tour_pack_ok", None) or {})
        packs[mode] = bool(ok)
        self._tour_pack_ok = packs
        self._pack_finished = True
        logger.warning(
            "PACK DONE {} span={:.2f}s ok={} tour_packs={} cycles={}/10",
            mode,
            span,
            ok,
            packs,
            int(getattr(self, "_qualifying_cycles", 0) or 0),
        )
        write_progress(
            last_pack_mode=mode,
            last_pack_seconds=span,
            last_pack_ok=ok,
            tour_pack_ok=packs,
            qualifying_cycles=int(getattr(self, "_qualifying_cycles", 0) or 0),
        )
        if all(item in packs for item in TOUR_MODE_IDS):
            if all(packs.get(item) for item in TOUR_MODE_IDS):
                self._qualifying_cycles = int(getattr(self, "_qualifying_cycles", 0) or 0) + 1
                logger.warning("TOUR CYCLE QUALIFIED {}/10", self._qualifying_cycles)
            else:
                logger.warning(
                    "TOUR CYCLE REJECTED a pack >40s — tally still {}/10 packs={}",
                    self._qualifying_cycles,
                    packs,
                )
            self._tour_pack_ok = {}
            write_progress(
                qualifying_cycles=self._qualifying_cycles,
                tour_pack_ok={},
            )
        self._pack_prep_started_at = None
        self._pack_send_times = []
        self._pack_mode = None

    def _mark_speed_proof(self) -> bool:
        times = getattr(self, "_pack_send_times", None) or getattr(self, "_burst_send_times", None) or []
        if len(times) < 5:
            return False
        span = float(times[-1] - times[0])
        logger.warning("SPEED window: last 5 spanned {:.2f}s (need ≤40 from prep)", span)
        return span <= 40.0

    def _speed_sleep(self, slow: float, fast: float = 0.05) -> None:
        CONTROL.sleep(fast if self._speed_burst_active() else slow)

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
        cached = getattr(self, "_cached_size", None)
        if cached:
            return cached
        image = capture_game_image(self.config, self.adb)
        if image is None:
            raise RuntimeError("Нет скрина BlueStacks — открой игру")
        self._cached_size = image.size
        return image.size

    def _plan_or_picker_open(self, image: Any | None = None) -> bool:
        """True while attack planning / unit picker is on screen."""
        try:
            shot = image if image is not None else self._image()
        except Exception:
            return False
        if find_picker_cards(shot):
            return True
        if is_formation_screen(shot) or is_travel_dialog(shot):
            return True
        if self._speed_burst_active():
            return False
        return bool(find_picker_confirm_button(shot))

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

    def _ruby_hud_close_allowed(self, nx: float, ny: float) -> bool:
        """Title-bar X of special offers may sit near ruby HUD; map ruby/+ must never be tapped."""
        if not is_ruby_plus_hud_point(nx, ny):
            return True
        try:
            image = self._image()
        except Exception:
            logger.warning("Не жму рубины/+ ({:.3f}, {:.3f}) — нет скрина", nx, ny)
            return False
        if is_quit_game_dialog(image):
            logger.warning("Не жму рубины/+ HUD ({:.3f}, {:.3f})", nx, ny)
            return False
        if is_formation_screen(image) and 0.90 <= nx <= 0.98 and ny <= 0.08:
            logger.info(
                "Экран формирования подтверждён — разрешаю его крестик ({:.3f}, {:.3f})",
                nx,
                ny,
            )
            return True
        if is_special_offers_screen(image):
            close_x, close_y = special_offers_close_point(image)
            if abs(nx - close_x) <= 0.06 and abs(ny - close_y) <= 0.10:
                return True
        if is_map_screen(image):
            logger.warning("Не жму рубины/+ HUD ({:.3f}, {:.3f})", nx, ny)
            return False
        logger.warning("Не жму рубины/+ HUD ({:.3f}, {:.3f})", nx, ny)
        return False

    def _rail_guard_allows(self, nx: float, ny: float) -> bool:
        """Block the special-offers rail unless this is that overlay's red X or the footer."""
        if not self._ruby_hud_close_allowed(nx, ny):
            return False
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
        if is_travel_dialog(shot) or is_formation_screen(shot) or self._picker_overlay_open(shot):
            return False
        action = popup_action(shot)
        closed_info = False
        if (
            action is not None
            and 0.68 <= float(action[0]) < 0.88
            and 0.10 <= float(action[1]) <= 0.22
            and not is_ruby_plus_hud_point(float(action[0]), float(action[1]))
        ):
            logger.warning(
                "Закрываю инфо-попап поверх магазина ({:.3f}, {:.3f})",
                float(action[0]),
                float(action[1]),
            )
            self._tap_norm_exact(float(action[0]), float(action[1]))
            CONTROL.sleep(0.35)
            closed_info = True
            try:
                shot = self._image()
            except Exception:
                return True
        offers = is_special_offers_screen(shot)
        shop_like = (not is_map_screen(shot)) and _center_parchment_ratio(shot) >= 0.40
        if not offers and not shop_like:
            return closed_info
        close_x, close_y = special_offers_close_point(shot)
        # Title-bar X is ~ (0.93, 0.04); mid-dialog «don't show» X is near y≈0.57.
        # Never the ruby/+ rail blob around (0.88, 0.15). Never inbox parchment.
        if is_inbox_screen(shot):
            return closed_info
        title_bar = close_y <= 0.10 and 0.88 <= close_x <= 0.97
        mid_dialog = 0.18 < close_y <= 0.68 and close_x < 0.92
        if is_ruby_plus_hud_point(close_x, close_y) and not title_bar:
            logger.warning("Спецпредложения: точка ({:.3f}, {:.3f}) совпала с рубинами — не жму", close_x, close_y)
            return closed_info
        if is_ruby_plus_hud_point(close_x, close_y) and title_bar:
            close_x, close_y = 0.823, 0.038
            logger.warning("Спецпредложения: крестик магазина сдвинут влево от ruby/+ ({:.3f}, {:.3f})", close_x, close_y)
        if (title_bar or mid_dialog or (close_x, close_y) == (0.823, 0.038)) and not is_green_hire_point(close_x, close_y):
            logger.info(
                "Закрываю спецпредложения красным крестиком ({:.3f}, {:.3f})",
                close_x,
                close_y,
            )
            self._tap_forced(close_x, close_y)
            CONTROL.sleep(0.35)
        logger.info("Спецпредложения — Escape/Back на карте не жму")
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

    def _dismiss_quit_game_if_open(self, image: Any | None = None) -> bool:
        """If «покинуть игру» is on screen, click НЕТ only — never ДА."""
        try:
            shot = image if image is not None else self._image()
        except Exception:
            return False
        if not is_quit_game_dialog(shot):
            return False
        point = find_quit_dialog_no_button(shot)
        if point[0] > 0.78:
            logger.warning("НЕТ слева, не жму правую ДА ({:.3f}, {:.3f})", point[0], point[1])
            point = (min(point[0], 0.68), point[1])
        logger.info("Выход из игры — жму НЕТ ({:.3f}, {:.3f}), ДА не трогаю", point[0], point[1])
        self._tap_forced(*point)
        CONTROL.sleep(0.45)
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
                for marker in (
                    "наград",
                    "reward",
                    "event",
                    "квест",
                    "quest",
                    "выполнен",
                    "пайцз",
                )
            )
            if is_green_hire_point(*point) and point[0] > 0.58 and not explicit_reward:
                logger.warning("Пропускаю зелёную печать найма ({:.3f}, {:.3f})", point[0], point[1])
                break
            logger.info("Награда — жму зелёную галочку ({:.3f}, {:.3f})", point[0], point[1])
            self._tap_forced(*point)
            clicked = True
            CONTROL.sleep(0.55)
        return clicked

    def _dismiss_ruby_shop_if_open(self, image: Any | None = None) -> bool:
        """Close «Добавить рубины». Never tap a price, cart, or subscribe."""
        try:
            shot = image if image is not None else self._image()
        except Exception:
            return False
        if not is_ruby_shop(shot):
            return False
        logger.info("Магазин рубинов — жму стрелку назад (0.070, 0.045), ничего не покупаю")
        self._tap_forced(0.070, 0.045)
        CONTROL.sleep(0.45)
        latest = self._image()
        if is_ruby_shop(latest):
            logger.info("Магазин рубинов ещё открыт — ещё раз стрелка, Escape на карте не жму")
            self._tap_forced(0.070, 0.045)
            CONTROL.sleep(0.45)
        return True

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
        """Close mail / «Удалить все» with the parchment X. Never confirm delete, never shop."""
        try:
            shot = image if image is not None else self._image()
        except Exception:
            return False
        if not is_inbox_screen(shot):
            return False
        close = find_parchment_title_close(shot)
        if close is None or close[1] > 0.14 or close[0] < 0.72 or is_ruby_plus_hud_point(*close):
            close = (0.86, 0.055)
        logger.info("Почта открыта — закрываю крестик ({:.3f}, {:.3f})", close[0], close[1])
        self._tap_forced(*close)
        CONTROL.sleep(0.45)
        latest = self._image()
        if is_inbox_screen(latest):
            logger.info("Почта ещё открыта — второй крестик (0.860, 0.055)")
            self._tap_forced(0.86, 0.055)
            CONTROL.sleep(0.45)
            latest = self._image()
        if is_inbox_screen(latest):
            logger.info("Почта не закрылась крестиком — ADB Back, Escape в окно не шлю")
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
        if is_travel_dialog(shot):
            return False
        if self._dismiss_overview_if_open(shot):
            return True
        if find_target_attack_button(shot) is not None:
            return False
        if self._map_has_attack_marks(shot):
            return False
        if self._dismiss_inbox_if_open(shot):
            return True
        close = find_parchment_title_close(shot)
        if close and close[0] < 0.90 and close[1] < 0.12 and not is_ruby_plus_hud_point(*close):
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
        if close[1] > 0.12 or close[0] >= 0.90 or is_ruby_plus_hud_point(*close):
            close = (0.823, 0.038)
        if is_ruby_plus_hud_point(*close):
            logger.warning("Налоги: крестик совпал с рубинами/+ — не жму")
            return False
        logger.info("Закрываю налоги только крестиком ({:.3f}, {:.3f})", close[0], close[1])
        self._tap_forced(*close)
        CONTROL.sleep(0.4)
        return True

    def _dismiss_overview_if_open(self, image: Any | None = None) -> bool:
        """Close encyclopedia «Обзор». Never tap «В закладки»."""
        try:
            shot = image if image is not None else self._image()
        except Exception:
            return False
        if self._plan_or_picker_open(shot):
            return False
        if not is_overview_plaque(shot):
            return False
        close = find_parchment_title_close(shot) or (0.823, 0.038)
        logger.info(
            "Обзор лагеря — закрываю крестик ({:.3f}, {:.3f}), закладки не жму",
            close[0],
            close[1],
        )
        self._tap_forced(*close)
        CONTROL.sleep(0.45)
        return True

    def _dismiss_blocking_overlay(self) -> bool:
        """Close a recognized blocker. Never tap offer-rail shop/chest buttons."""
        if self._dismiss_quit_game_if_open():
            return True
        if self._dismiss_connection_error_if_open():
            return True
        if self._dismiss_ruby_shop_if_open():
            return True
        if self._dismiss_special_offers_if_open():
            return True
        if self._dismiss_overview_if_open():
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
        vision = (getattr(self, "config", None) or {}).get("vision") or {}
        settle_min = float(vision.get("click_settle_min_seconds") or 0.04)
        settle_max = float(vision.get("click_settle_max_seconds") or 0.06)
        time.sleep(random.uniform(min(settle_min, settle_max), max(settle_min, settle_max)))

    def _tap_norm_exact(self, nx: float, ny: float) -> None:
        if not self._rail_guard_allows(nx, ny):
            return
        size = self._size()
        x, y = _abs_point(size, [nx, ny])
        self.adb.tap(x, y, source_size=size)
        vision = (getattr(self, "config", None) or {}).get("vision") or {}
        time.sleep(float(vision.get("exact_click_settle_seconds") or 0.05))

    def _tap_forced(self, nx: float, ny: float) -> None:
        """Click a known overlay control. Still never the map ruby/+ HUD."""
        if not self._ruby_hud_close_allowed(nx, ny):
            return
        size = self._size()
        x, y = _abs_point(size, [nx, ny])
        self.adb.tap(x, y, source_size=size)
        vision = (getattr(self, "config", None) or {}).get("vision") or {}
        time.sleep(float(vision.get("forced_click_settle_seconds") or 0.05))

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
        formation_close = 0.90 <= point[0] <= 0.98 and point[1] <= 0.08
        if is_ruby_plus_hud_point(*point) and not formation_close:
            parchment = find_parchment_title_close(image)
            if parchment and not is_ruby_plus_hud_point(*parchment):
                point = parchment
                logger.warning("Крестик плана совпал с рубинами/+ — жму пергамент ({:.3f}, {:.3f})", point[0], point[1])
            else:
                logger.warning("Крестик плана совпал с рубинами/+ — не жму")
                return False
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
        confirm = find_picker_confirm_button(image) is not None
        if self._speed_burst_active():
            return bool(confirm and _center_parchment_ratio(image) > 0.28)
        if confirm:
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
        self._pack_empty_wave_just_seen = True
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
        self._picker_max_adjusted = False
        self.tap_rel(slot_key)
        timeout = 1.6 if self._speed_burst_active() else self._vision_seconds(
            "picker_open_timeout_seconds", 8
        )
        picker = self._wait_for(
            self._picker_overlay_open,
            timeout=timeout,
            label="открытие пикера",
        )
        if picker is None and is_formation_screen(self._image()):
            slots = find_formation_unit_slots(self._image())
            if slots:
                logger.warning(
                    "Пикер не открылся с unit_slot — жму найденный слот ({:.3f},{:.3f})",
                    slots[0][0],
                    slots[0][1],
                )
                self._tap_norm_exact(*slots[0])
                picker = self._wait_for(
                    self._picker_overlay_open,
                    timeout=1.1 if self._speed_burst_active() else timeout,
                    label="открытие пикера слотом",
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
        if not self._speed_burst_active():
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
        point = diagnostic["point"]
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
            lambda img: self._is_plain_formation(img)
            or (
                self._positive_unit_fill(observed_fill)
                and find_formation_attack_button(img) is not None
            ),
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
        if self._positive_unit_fill(observed_fill):
            if find_formation_attack_button(latest) is not None:
                logger.info(
                    "После галочки видна кнопка Нападение — пикер подтверждён, другого солдата не беру"
                )
                save_shot(latest, "unit-picker-confirm-after.png")
                return True, "confirmed", latest
            if leftover and leftover[1] > 0 and leftover[0] >= leftover[1]:
                extra = self._picker_confirm_point(latest)
                if extra is not None:
                    logger.info(
                        "После галочки пикер всё ещё {}/{} — повторно жму OK, карту солдата не меняю",
                        leftover[0],
                        leftover[1],
                    )
                    self._tap_norm_exact(*extra)
                    after2 = self._wait_for(
                        self._is_plain_formation,
                        timeout=5,
                        label="повтор галочки пикера",
                    )
                    latest = after2 if after2 is not None else self._image()
                logger.info(
                    "Пикер {}/{} после OK — считаю подтверждённым, другого солдата не выбираю",
                    leftover[0],
                    leftover[1],
                )
                save_shot(latest, "unit-picker-confirm-after.png")
                return True, "confirmed", latest
        save_shot(latest, "unit-picker-confirm-transition-failed.png")
        return False, "unit_picker_confirm_transition_failed", None

    def diagnose_movement_confirm(self, click: bool = False) -> tuple[bool, str, Any | None]:
        """Validate the distinct final movement confirmation before sending."""
        image = self._image()
        diagnostic = movement_confirm_diagnostics(image)
        point = diagnostic["point"]
        if not self._speed_burst_active():
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
        if click and self._speed_burst_active():
            self._tap_norm_exact(*point)
            closed = self._wait_for(
                lambda img: (not is_travel_dialog(img)) and (not is_formation_screen(img)),
                timeout=0.35,
                label="закрытие похода",
            )
            return True, "confirmed", closed if closed is not None else image
        self._tap_norm_exact(*point)
        closed = self._wait_for(
            lambda img: (not is_travel_dialog(img)) and (not is_formation_screen(img)),
            timeout=1.2 if self._speed_burst_active() else 10,
            label="закрытие похода",
        )
        latest = closed if closed is not None else self._image()
        if self._dismiss_no_commanders(latest):
            return False, "no_commanders", None
        if is_travel_dialog(latest) or is_formation_screen(latest):
            failed = latest
            if not self._speed_burst_active():
                save_shot(failed, "movement-confirm-transition-failed.png")
            return False, "movement_confirm_transition_failed", None
        if not self._speed_burst_active():
            save_shot(latest, "movement-confirm-after.png")
        return True, "confirmed", latest

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
        CONTROL.sleep(0.12 if self._speed_burst_active() else 0.6)

    def scroll_tool_inventory(self, stride: float = 0.04, upward: bool = False) -> None:
        """One inventory row, then caller screenshots. Never shop/search."""
        size = self._size()
        cx, cy = 0.40, 0.56
        x, y = _abs_point(size, [cx, cy])
        logger.info(
            "Скролл орудий ({:.3f}, {:.3f}) stride={:.3f} {}",
            cx,
            cy,
            float(stride),
            "вверх" if upward else "вниз",
        )
        wheel = 120 if upward else -120
        self.adb.wheel(x, y, delta=wheel, source_size=size)
        width, height = size
        span = max(0.03, min(0.05, float(stride)))
        start_y = 0.62 if upward else 0.57
        end_y = (start_y - span) if upward else min(0.70, start_y + span)
        self.adb.swipe(
            round(0.40 * width),
            round(start_y * height),
            round(0.40 * width),
            round(end_y * height),
            duration_ms=450,
            source_size=size,
        )
        CONTROL.sleep(0.55)

    def reset_tool_inventory_scroll(self, stride: float = 0.04) -> None:
        for _ in range(5):
            self.scroll_tool_inventory(stride=stride, upward=True)

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
            if (point[0] - bx) ** 2 + (point[1] - by) ** 2 < 0.035**2:
                return True
        return False

    def _unblock_screen_target(self, point: tuple[float, float] | None) -> None:
        if point is None:
            return
        blocked = getattr(self, "_blocked_screen_targets", None)
        if not blocked:
            return
        self._blocked_screen_targets = [
            (bx, by)
            for bx, by in blocked
            if (point[0] - bx) ** 2 + (point[1] - by) ** 2 >= 0.035**2
        ]

    def _block_screen_target(self, point: tuple[float, float]) -> None:
        if self._is_blocked_screen_target(point):
            return
        self._blocked_screen_targets.append((float(point[0]), float(point[1])))
        logger.info(
            "Точка ({:.3f}, {:.3f}) без таблички — больше не жму",
            point[0],
            point[1],
        )

    def _jump_to_coords(self, coords: tuple[int, int]) -> None:
        logger.warning(
            "Переход через поиск {}:{} отключён — кнопка search на рейке спецпредложений",
            coords[0],
            coords[1],
        )

    def _recenter_on_main_castle(self, image: Any) -> Any:
        """Keep the hunt anchored on the account's MAIN castle, not the last target."""
        if self._dismiss_quit_game_if_open(image):
            image = self._image()
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
        if self._dismiss_quit_game_if_open(image):
            image = self._image()
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
        burst = self._speed_burst_active()
        logger.info("Игра грузит игровой сервер — жду карту, не жму магазин и поиск")
        deadline = time.time() + (min(timeout, 2.0) if burst else timeout)
        while time.time() < deadline:
            CONTROL.sleep(0.15 if burst else 2.0)
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

    def _map_has_attack_marks(self, image: Any) -> bool:
        if (
            find_samurai_candidates(image)
            or find_nomad_candidates(image)
            or find_robber_candidates(image)
        ):
            return True
        kind = str((self.config or {}).get("current_target_kind") or "")
        if is_world_npc_kind(kind) and kind != "baron":
            return bool(find_world_castle_candidates(image, kind))
        return False

    def _hunt_quota(self) -> int:
        """How many unique map marks to collect before attacking — not a commander cap."""
        kind = str((self.config or {}).get("current_target_kind") or "")
        if kind in {"samurai", "nomad"}:
            return 4
        if is_world_npc_kind(kind):
            return 5
        return DEFAULT_HUNT_BATCH

    def _await_world_map(self, timeout: float = 6.0) -> Any | None:
        """Wait for the kingdom map without ESC/formation_close after a successful plan."""
        if self._speed_burst_active():
            image = self._image()
            if is_map_screen(image):
                return image
            return self._wait_for(is_map_screen, timeout=min(float(timeout), 2.0), label="карта")
        image = self._image()
        if self._dismiss_quit_game_if_open(image):
            CONTROL.sleep(0.3)
            image = self._image()
        if is_map_screen(image) and self._map_has_attack_marks(image):
            return image
        if self._dismiss_inbox_if_open(image):
            CONTROL.sleep(0.3)
            image = self._image()
            if is_map_screen(image) and self._map_has_attack_marks(image):
                return image
        if self._dismiss_blocking_menu_if_no_camps(image):
            CONTROL.sleep(0.3)
            image = self._image()
            if is_map_screen(image) and self._map_has_attack_marks(image):
                return image
        if is_map_screen(image):
            if self._dismiss_taxes_if_open(image) or self._dismiss_special_offers_if_open(image):
                CONTROL.sleep(0.35)
                image = self._image()
            if is_map_screen(image) and self._map_has_attack_marks(image):
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

    def _world_map_npc_point(self, point: tuple[float, float]) -> bool:
        """Reject HUD / nav chrome that robber-template matching treats as towers."""
        nx, ny = float(point[0]), float(point[1])
        if ny > 0.80 or ny < 0.16:
            return False
        if nx < 0.12 or nx > 0.88:
            return False
        if is_ruby_plus_hud_point(nx, ny):
            return False
        return True

    def _spread_world_hunt_targets(self, targets: list[HuntTarget]) -> list[HuntTarget]:
        picked: list[HuntTarget] = []
        for target in targets:
            if any(
                (target.point[0] - other.point[0]) ** 2
                + (target.point[1] - other.point[1]) ** 2
                < 0.05**2
                for other in picked
            ):
                continue
            picked.append(target)
        picked.sort(
            key=lambda item: (item.point[0] - 0.50) ** 2 + (item.point[1] - 0.54) ** 2,
            reverse=True,
        )
        return picked

    def _probe_world_npc_target(self, kind: str, target: HuntTarget) -> bool:
        """Tap a map mark; keep it only if the plaque is this world's tower/fort."""
        point = target.point
        if not self._world_map_npc_point(point) or self._is_blocked_screen_target(point):
            return False
        logger.info("Пробую метку мира ({:.3f}, {:.3f})", point[0], point[1])
        self._tap_norm(*point)
        CONTROL.sleep(0.12 if self._speed_burst_active() else 0.55)
        image = self._image()
        if is_travel_dialog(image) or self._picker_overlay_open(image):
            logger.info("Проба мира открыла план — беру эту цель")
            return True
        if (
            is_formation_screen(image)
            and find_world_parchment_attack_button(image) is None
            and not is_info_plaque(image)
        ):
            logger.info("Проба мира открыла план — беру эту цель")
            return True
        if is_overview_plaque(image):
            logger.info("Проба мира: Обзор, не башня")
            self._dismiss_overview_if_open(image)
            self._block_screen_target(point)
            return False
        has_plaque = (
            is_info_plaque(image)
            or find_plaque_attack_button(image) is not None
            or find_target_attack_button(image) is not None
        )
        if not has_plaque:
            logger.info("Проба мира: таблички нет")
            self._block_screen_target(point)
            return False
        if not self._world_target_title_ok(image, kind):
            logger.info("Проба мира: не башня этого мира — закрываю")
            save_shot(image, f"world-title-rejected-{kind}.png")
            self._dismiss_wrong_world_target(image)
            self._block_screen_target(point)
            return False
        logger.info("Проба мира: табличка башни/форта совпала")
        return True

    def _pick_probed_world_target(self, kind: str, targets: list[HuntTarget]) -> HuntTarget | None:
        if not targets:
            return None
        spread = self._spread_world_hunt_targets(targets)
        if world_hunt_hits_are_cluster([item.point for item in spread], None):
            logger.info(
                "На экране {} меток одним комком — пробую одну, не всю пачку замков",
                len(targets),
            )
            spread = spread[:1]
        else:
            spread = spread[:3]
        for item in spread:
            if self._probe_world_npc_target(kind, item):
                return item
        return None

    def _list_eligible_targets(
        self,
        image: Any,
        kind: str,
        include_blocked: bool = False,
    ) -> list[HuntTarget]:
        if kind == "samurai":
            threshold = float((self.config.get("vision") or {}).get("samurai_threshold") or 0.65)
            candidates = find_samurai_candidates(image, threshold)
            skip_burning = True
        elif kind == "nomad":
            threshold = float((self.config.get("vision") or {}).get("nomad_threshold") or 0.65)
            candidates = find_nomad_candidates(image, threshold)
            skip_burning = True
        elif is_world_npc_kind(kind) and kind != "baron":
            threshold = float(
                (self.config.get("vision") or {}).get("world_castle_threshold") or 0.62
            )
            if self._speed_burst_active():
                threshold = min(threshold, 0.47)
            candidates = find_world_castle_candidates(image, kind, threshold)
            skip_burning = True
        else:
            threshold = float((self.config.get("vision") or {}).get("robber_threshold") or 0.65)
            candidates = find_robber_candidates(image, threshold)
            # Snow/sand/lava false-triggers the baron "burning" filter.
            skip_burning = is_world_npc_kind(kind)
        if not candidates:
            return []
        if self._speed_burst_active():
            items = [
                HuntTarget((item[0], item[1]), None)
                for item in candidates
                if not is_offer_rail_point(item[0], item[1])
                and not self._is_blocked_screen_target((item[0], item[1]))
                and (skip_burning or not is_burning_candidate(image, (item[0], item[1])))
                and (
                    kind == "baron"
                    or not is_world_npc_kind(kind)
                    or self._world_map_npc_point((item[0], item[1]))
                )
            ]
            items.sort(key=lambda item: (item.point[0] - 0.50) ** 2 + (item.point[1] - 0.54) ** 2)
            return items
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
                if kind != "baron" and is_world_npc_kind(kind) and not self._world_map_npc_point(point):
                    continue
                if not include_blocked and self._is_blocked_screen_target(point):
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
        chrome = 0
        for candidate in candidates:
            point = (candidate[0], candidate[1])
            if not skip_burning and is_burning_candidate(image, point):
                burning += 1
                continue
            if is_offer_rail_point(point[0], point[1]):
                chrome += 1
                continue
            if kind != "baron" and is_world_npc_kind(kind) and not self._world_map_npc_point(point):
                chrome += 1
                continue
            if not include_blocked and self._is_blocked_screen_target(point):
                blocked += 1
                continue
            coords = project_map_coordinate(
                point,
                viewport,
                (float(anchor_raw[0]), float(anchor_raw[1])),
                (float(scale_raw[0]), float(scale_raw[1])),
            )
            stable = (round(coords[0]), round(coords[1]))
            if kind == "nomad":
                snapped = self.store.canonicalize_nomad_coords(stable)
                if snapped:
                    stable = snapped
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
                "на перезарядке {}, заблокировано {}, chrome {}",
                len(candidates),
                burning,
                cooling,
                blocked,
                chrome,
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
        if self._speed_burst_active() and is_world_npc_kind(kind):
            image = self._image()
            if is_inbox_screen(image):
                self._dismiss_inbox_if_open(image)
                image = self._image()
            if not is_map_screen(image):
                return []
            if kind == "baron" and map_grass_ratio(image) < 0.12:
                return []
            return self._list_eligible_targets(image, kind)[:1]

        def ingest(image: Any) -> bool:
            if (
                not is_map_screen(image)
                or is_ruby_shop(image)
                or is_special_offers_screen(image)
                or is_overview_plaque(image)
            ):
                logger.info("Скан карты остановлен — экран больше не карта")
                return True
            for target in self._list_eligible_targets(image, kind):
                if kind == "nomad" and target.coords:
                    snapped = self.store.canonicalize_nomad_coords(target.coords)
                    if snapped:
                        target = HuntTarget(target.point, snapped)
                    if any(
                        existing.coords
                        and abs(existing.coords[0] - target.coords[0]) <= 4
                        and abs(existing.coords[1] - target.coords[1]) <= 4
                        for existing in found
                    ):
                        continue
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
                if kind == "nomad" and len(found) == 1:
                    self._last_nomad_point = target.point
                    self._nomad_recenter_next = False
                    if target.coords:
                        self._selected_target_coords = target.coords
                if len(found) >= quota:
                    return True
            return False

        image = self._image()
        if self._dismiss_blocking_overlay() or self._dismiss_reward_popups(image):
            CONTROL.sleep(0.45)
            image = self._image()
        if self._dismiss_inbox_if_open(image):
            CONTROL.sleep(0.4)
            image = self._image()
        if (
            not is_map_screen(image)
            or is_ruby_shop(image)
            or is_special_offers_screen(image)
            or is_overview_plaque(image)
        ):
            logger.info("Охота: экран не чистая карта — цели не ищу")
            return []
        if kind == "baron" and map_grass_ratio(image) < 0.12:
            logger.info("Охота баронов: нет травы Великой империи — не сканирую чужой мир")
            self._need_ge_home = True
            self._switched_world_id = None
            return []
        if is_info_plaque(image) or find_target_attack_button(image) is not None or is_world_npc_kind(kind):
            self._deselect_via_safe_grass()
            image = self._image()
        if not is_world_npc_kind(kind):
            image = self._recenter_on_main_castle(image)
            if not is_map_screen(image) and self._dismiss_blocking_overlay():
                CONTROL.sleep(0.4)
                image = self._recenter_on_main_castle(self._image())
        if is_world_npc_kind(kind):
            probed = self._pick_probed_world_target(
                kind, self._list_eligible_targets(image, kind)
            )
            if probed is not None:
                logger.info("Пачка охоты: проверенная башня/форт, скан дальше не нужен")
                return [probed]
        elif ingest(image):
            logger.info("Пачка охоты готова: {} целей, полный скан больше не нужен", len(found))
            return found
        if found and not is_world_npc_kind(kind):
            logger.info(
                "На экране уже {} целей — бью их до скана остальной карты",
                len(found),
            )
            return found
        if not is_world_npc_kind(kind):
            for attempt in range(2):
                logger.info(
                    "У замка лагерей не видно — закрываю окна и центрирую ещё раз ({}/2)",
                    attempt + 1,
                )
                self._dismiss_blocking_overlay()
                CONTROL.sleep(0.4)
                image = self._recenter_on_main_castle(self._image())
                if ingest(image):
                    return found
                if found:
                    logger.info(
                        "После повторного центра нашёл {} лагерей — карту не листаю",
                        len(found),
                    )
                    return found
        for dx, dy in self._map_scan_offsets():
            logger.info("Скан карты: сдвиг ({:+.2f}, {:+.2f})", dx, dy)
            self._pan_map(dx, dy)
            CONTROL.sleep(0.25)
            image = self._image()
            if is_world_npc_kind(kind):
                self._blocked_screen_targets.clear()
                probed = self._pick_probed_world_target(
                    kind, self._list_eligible_targets(image, kind)
                )
                if probed is not None:
                    logger.info(
                        "Оставляю сдвиг карты, бью проверенную цель мира без возврата к замку GE"
                    )
                    return [probed]
                self._pan_map(-dx, -dy)
                CONTROL.sleep(0.15)
                continue
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
        last = getattr(self, "_last_nomad_point", None)
        items = self._list_eligible_targets(
            image,
            kind,
            include_blocked=(kind == "nomad" and target.coords is not None),
        )
        if kind == "nomad" and target.coords is not None:
            for item in items:
                if is_burning_candidate(image, item.point):
                    continue
                if item.coords and (
                    abs(item.coords[0] - target.coords[0]) <= 4
                    and abs(item.coords[1] - target.coords[1]) <= 4
                ):
                    logger.info(
                        "Тот же лагерь {} на экране ({:.3f}, {:.3f}) — очередь {}",
                        item.coords,
                        item.point[0],
                        item.point[1],
                        target.coords,
                    )
                    return item.point
            # Coords missing on screen: only the last click of THIS camp, never a yurt near map center.
            expected = last if last and not _dummy_center(last) else (
                None if _dummy_center(target.point) else target.point
            )
            if expected is None:
                return None
            nearest: tuple[float, HuntTarget] | None = None
            for item in items:
                if is_burning_candidate(image, item.point):
                    continue
                dist2 = (item.point[0] - expected[0]) ** 2 + (item.point[1] - expected[1]) ** 2
                if dist2 < 0.04**2 and (nearest is None or dist2 < nearest[0]):
                    nearest = (dist2, item)
            if nearest is not None:
                logger.info(
                    "Тот же лагерь {} по точке очереди ({:.3f}, {:.3f})",
                    target.coords,
                    nearest[1].point[0],
                    nearest[1].point[1],
                )
                return nearest[1].point
            return None
        expected = target.point
        screen_tol = 0.04
        for item in items:
            if is_burning_candidate(image, item.point):
                continue
            if target.coords and item.coords:
                if (
                    abs(item.coords[0] - target.coords[0]) <= 4
                    and abs(item.coords[1] - target.coords[1]) <= 4
                ):
                    return item.point
            dist2 = (item.point[0] - expected[0]) ** 2 + (
                item.point[1] - expected[1]
            ) ** 2
            if dist2 < screen_tol**2:
                return item.point
        if kind == "samurai":
            nearest = None
            for item in items:
                if is_burning_candidate(image, item.point):
                    continue
                dist2 = (item.point[0] - target.point[0]) ** 2 + (
                    item.point[1] - target.point[1]
                ) ** 2
                if nearest is None or dist2 < nearest[0]:
                    nearest = (dist2, item)
            if nearest is not None:
                logger.info(
                    "Лагерь на экране ({:.3f}, {:.3f}), dist={:.3f}",
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
        if self._speed_burst_active():
            image = self._image()
        else:
            image = self._await_world_map()
        if image is None:
            return None
        if not self._speed_burst_active() and self._dismiss_special_offers_if_open(image):
            image = self._await_world_map(timeout=4) or self._image()
        self._selected_target_coords = target.coords
        if kind == "nomad":
            self._unblock_screen_target(target.point)
            self._unblock_screen_target(getattr(self, "_last_nomad_point", None))
            if target.point and not _dummy_center(target.point) and getattr(self, "_last_nomad_point", None) is None:
                self._last_nomad_point = target.point
        visible = self._match_visible_target(image, kind, target)
        if visible:
            if not self._selected_target_coords:
                self._selected_target_coords = target.coords
            if kind == "nomad":
                self._last_nomad_point = visible
                logger.info(
                    "Очередь лагеря {} — жму совпавшую юрту ({:.3f}, {:.3f})",
                    target.coords,
                    visible[0],
                    visible[1],
                )
            return visible
        if kind == "nomad" and target.coords is not None:
            queued = getattr(self, "_last_nomad_point", None)
            if queued is None or _dummy_center(queued):
                queued = None if _dummy_center(target.point) else target.point
            if not getattr(self, "_nomad_recenter_next", False) and queued:
                logger.info(
                    "Жму очередь того же лагеря {} ({:.3f}, {:.3f}), чужой не беру",
                    target.coords,
                    queued[0],
                    queued[1],
                )
                self._last_nomad_point = queued
                return queued
            logger.info("Лагерь {} не совпал на экране — центрирую замок, чужой не беру", target.coords)
            self._nomad_recenter_next = False
            image = self._recenter_on_main_castle(image)
            visible = self._match_visible_target(image, kind, target)
            if visible:
                self._last_nomad_point = visible
                logger.info(
                    "После центра очередь {} — жму ту же юрту ({:.3f}, {:.3f})",
                    target.coords,
                    visible[0],
                    visible[1],
                )
                return visible
            if queued:
                logger.info(
                    "После центра жму очередь того же лагеря {} ({:.3f}, {:.3f}), чужой не беру",
                    target.coords,
                    queued[0],
                    queued[1],
                )
                self._last_nomad_point = queued
                return queued
            logger.info("Лагерь {} не на экране — очередь не сбрасываю", target.coords)
            return None
        if kind == "samurai":
            finder = find_samurai_candidates

            def visible_camps(shot: Any) -> list[tuple[float, float, float]]:
                return [
                    camp
                    for camp in finder(shot)
                    if not self._is_blocked_screen_target((float(camp[0]), float(camp[1])))
                ]

            camps = visible_camps(image)
            if not camps:
                logger.info("Видимых лагерей нет — центрирую замок и смотрю ещё раз")
                image = self._recenter_on_main_castle(image)
                camps = visible_camps(image)
            if camps:
                point = (float(camps[0][0]), float(camps[0][1]))
                logger.info(
                    "Беру видимый лагерь без сверки координат ({:.3f}, {:.3f})",
                    point[0],
                    point[1],
                )
                return point
            logger.info("Видимых лагерей нет — не панорамирую и не открываю поиск")
        elif is_world_npc_kind(kind):
            point = target.point
            if point and self._world_map_npc_point(point):
                logger.info(
                    "Жму цель мира ({:.3f}, {:.3f}) без центра главного замка GE",
                    point[0],
                    point[1],
                )
                return point
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
        self._cached_size = image.size
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
        if self._speed_burst_active():
            timeout = min(timeout, 2.5)
            heartbeat = min(float(heartbeat), 0.7)
        deadline = time.time() + timeout
        started = time.time()
        next_heartbeat = started + max(0.3, float(heartbeat))
        poll = 0.02 if self._speed_burst_active() else 0.35
        while time.time() < deadline:
            CONTROL.check()
            image = self._image()
            if predicate(image):
                return image
            if label and time.time() >= next_heartbeat:
                logger.info("Пикер: жду {}… ({:.0f}s)", label, time.time() - started)
                next_heartbeat = time.time() + max(0.5, float(heartbeat))
            CONTROL.sleep(poll)
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

    def _world_attack_button_safe(self, found: tuple[float, float]) -> bool:
        """World plaque «Нападение» is lower-right (~0.76, 0.63). Never ruby/+ or the offer rail."""
        nx, ny = float(found[0]), float(found[1])
        if is_ruby_plus_hud_point(nx, ny) or is_offer_rail_point(nx, ny):
            return False
        return 0.30 <= nx <= 0.82 and 0.28 <= ny <= 0.72

    def _open_formation(self, point: tuple[float, float], kind: str) -> bool:
        already = self._image()
        if find_picker_cards(already):
            logger.info("Пикер уже открыт — не закрываю и не жму карту")
            return True
        if not self._speed_burst_active():
            if is_travel_dialog(already):
                logger.info("Планирование уже открыто — не закрываю и не жму карту")
                return True
        world_attack = (
            find_world_parchment_attack_button(already) if is_world_npc_kind(kind) else None
        )
        if (
            not self._speed_burst_active()
            and self._plan_or_picker_open(already)
            and world_attack is None
            and not (is_world_npc_kind(kind) and is_info_plaque(already))
        ):
            logger.info("Планирование уже открыто — не закрываю и не жму карту")
            return True
        plaque_already = is_world_npc_kind(kind) and is_info_plaque(already) and (
            find_plaque_attack_button(already) is not None
            or find_world_parchment_attack_button(already) is not None
        )
        if plaque_already:
            logger.info("Табличка мира уже открыта — не жму метку повторно")
            popup = already
        else:
            self._tap_norm(*point)
        if kind in {"samurai", "nomad"}:
            plaque_ready = lambda image: (
                is_info_plaque(image)
                or find_plaque_attack_button(image) is not None
                or find_target_attack_button(image) is not None
                or is_overview_plaque(image)
                or is_travel_dialog(image)
                or is_difficulty_dialog(image)
                or is_formation_screen(image)
            )
            popup = self._wait_for(plaque_ready, timeout=1.2 if self._speed_burst_active() else 3, label="табличка лагеря")
            if popup is None:
                lower = (float(point[0]), min(0.80, float(point[1]) + 0.04))
                logger.info("Табличка не вышла — жму чуть ниже лагеря ({:.3f}, {:.3f})", lower[0], lower[1])
                self._tap_norm(*lower)
                popup = self._wait_for(plaque_ready, timeout=1.5 if self._speed_burst_active() else 5, label="табличка после повторного клика")
            if popup is not None and self._plan_or_picker_open(popup):
                return True
            if popup is not None and is_overview_plaque(popup):
                logger.info("Открылся обзор лагеря, не Нападение — закрываю")
                self._dismiss_overview_if_open(popup)
                return False
            if popup is None or (
                not is_info_plaque(popup)
                and find_plaque_attack_button(popup) is None
                and find_target_attack_button(popup) is None
                and not is_difficulty_dialog(popup)
                and not is_formation_screen(popup)
                and not is_overview_plaque(popup)
                and not is_travel_dialog(popup)
                and find_travel_seal_pair(popup) is None
            ):
                blocked = self._image()
                if (
                    self._plan_or_picker_open(blocked)
                    or is_travel_dialog(blocked)
                    or find_travel_seal_pair(blocked) is not None
                ):
                    logger.info("После клика открыт поход/план — лагерь не блокирую")
                    return True
                if kind != "nomad":
                    self._block_screen_target(point)
                action = popup_action(blocked)
                if action:
                    logger.info("Цель перекрыта окном; закрываю его перед повтором")
                    self._tap_norm(*action)
                return False
        else:
            popup = self._wait_for(
                lambda image: (
                    find_target_attack_button(image) is not None
                    or find_plaque_attack_button(image) is not None
                    or is_info_plaque(image)
                    or is_formation_screen(image)
                    or is_travel_dialog(image)
                ),
                timeout=1.2 if self._speed_burst_active() else 5,
                label="табличка мира",
            )
            if popup is None:
                blocked = self._image()
                if self._plan_or_picker_open(blocked) and not is_info_plaque(blocked):
                    return True
                action = popup_action(blocked)
                if action:
                    logger.info("Цель перекрыта окном; закрываю его перед повтором")
                    self._tap_norm(*action)
                return False
            if self._plan_or_picker_open(popup) and not (
                is_world_npc_kind(kind)
                and (
                    is_info_plaque(popup)
                    or find_world_parchment_attack_button(popup) is not None
                )
            ):
                return True
        if is_world_npc_kind(kind) and popup is not None:
            if not self._world_target_title_ok(popup, kind):
                logger.info("Имя цели не совпало с башней/фортом этого мира — закрываю табличку")
                self._dismiss_wrong_world_target(popup)
                self._block_screen_target(point)
                return False
        if not self._speed_burst_active():
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
                if kind == "nomad" and self._selected_target_coords:
                    logger.info(
                        "Табличка дала ({}, {}) — оставляю очередь лагеря {}",
                        target_x,
                        target_y,
                        self._selected_target_coords,
                    )
                elif (
                    kind in {"samurai", "nomad"}
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
        if kind in {"samurai", "nomad"}:
            title = ocr_text_ui(crop_rel(popup, [0.18, 0.14, 0.82, 0.32]), psm=6)
            body = ocr_text_ui(crop_rel(popup, [0.18, 0.20, 0.82, 0.55]), psm=6)
            blob = f"{title} {body}"
            level = parse_samurai_camp_level(blob)
            logger.info("OCR уровня лагеря: {} / {}", level, blob[:80])
            try:
                coords = self._selected_target_coords or (0, 0)
                save_shot(popup, f"{kind}_level_{coords[0]}_{coords[1]}.png")
            except Exception:
                pass
            if level is not None and self._selected_target_coords:
                if kind == "nomad":
                    remaining = remaining_attacks_from_nomad_level(level)
                    remaining = self.store.apply_nomad_ocr_remaining(
                        self._selected_target_coords, remaining
                    )
                else:
                    remaining = remaining_attacks_from_level(level)
                    self.store.set_samurai_remaining(self._selected_target_coords, remaining)
                if remaining is not None and remaining <= 0:
                    logger.info("Лагерь {} без атак — закрываю табличку", self._selected_target_coords)
                    self.tap_rel("map")
                    return False
        attack_point = None
        if kind in {"samurai", "nomad"}:
            if is_info_plaque(popup):
                attack_point = find_plaque_attack_button(popup)
                if attack_point is None:
                    attack_point = (0.50, 0.60)
                    logger.info("На пергаменте нет золотой кнопки — жму Нападение по центру таблички")
                else:
                    logger.info("Нападение на табличке ({:.3f}, {:.3f})", attack_point[0], attack_point[1])
            else:
                attack_point = find_target_attack_button(popup, near=point)
                if attack_point is None:
                    logger.info("Нет таблички и нет радиала Напасть рядом с лагерем — лагерь не выкидываю")
                    if kind != "nomad":
                        self._block_screen_target(point)
                    return False
                logger.info(
                    "Радиал Напасть ({:.3f}, {:.3f}) — не Обзор и не шпионаж",
                    attack_point[0],
                    attack_point[1],
                )
        else:
            attack_point = find_target_attack_button(popup)
        if not is_world_npc_kind(kind):
            if attack_point is None:
                return False
            self._tap_norm(*attack_point)
            time.sleep(0.8)
        if kind in {"samurai", "nomad"}:
            opened = self._wait_for(
                lambda img: is_difficulty_dialog(img)
                or is_formation_screen(img)
                or is_no_commanders_parchment(img)
                or is_travel_dialog(img),
                timeout=10,
                label="сложность или план лагеря",
            )
            shot = opened if opened is not None else self._image()
            if self._dismiss_no_commanders(shot):
                return False
            if is_difficulty_dialog(shot):
                logger.info("Окно «выберите сложность» — не жду план, отдам модулю")
                return True
            if is_formation_screen(shot) or is_travel_dialog(shot):
                return True
            fresh = self._image()
            retry = (
                find_plaque_attack_button(fresh)
                if is_info_plaque(fresh)
                else find_target_attack_button(fresh, near=point)
            )
            if retry is not None:
                logger.info("План не открылся — ещё раз жму Нападение ({:.3f}, {:.3f})", retry[0], retry[1])
                self._tap_norm(*retry)
                opened = self._wait_for(
                    lambda img: is_difficulty_dialog(img)
                    or is_formation_screen(img)
                    or is_no_commanders_parchment(img)
                    or is_travel_dialog(img),
                    timeout=10,
                    label="повтор таблички лагеря",
                )
                shot = opened if opened is not None else self._image()
                if self._dismiss_no_commanders(shot):
                    return False
                if is_difficulty_dialog(shot) or is_formation_screen(shot) or is_travel_dialog(shot):
                    return True
            if is_travel_dialog(self._image()):
                logger.info("После Напасть открыт диалог похода — план отдаю модулю")
                return True
            logger.warning(
                "Табличка лагеря есть, план не открылся — слепую точку start_attack_confirm не жму"
            )
            return False
        if is_world_npc_kind(kind):
            found_points: list[tuple[float, float]] = []
            for finder in (
                find_plaque_attack_button,
                find_world_parchment_attack_button,
                find_target_attack_button,
            ):
                found = finder(popup)
                if found is None or not self._world_attack_button_safe(found):
                    continue
                if any((found[0] - other[0]) ** 2 + (found[1] - other[1]) ** 2 < 0.012**2 for other in found_points):
                    continue
                found_points.append(found)
            if not found_points:
                logger.warning("Без визуально подтверждённой кнопки Нападение мира ничего не жму")
                self._deselect_via_safe_grass()
                return False
            plan_timeout = 2.0 if self._speed_burst_active() else 8
            for attack_point in found_points:
                logger.info("Нападение мира ({:.3f}, {:.3f})", attack_point[0], attack_point[1])
                self._tap_norm_exact(*attack_point)
                opened = self._wait_for(
                    lambda img: is_formation_screen(img)
                    or is_no_commanders_parchment(img)
                    or is_travel_dialog(img),
                    timeout=plan_timeout,
                    label="план мира",
                )
                shot = opened if opened is not None else self._image()
                if self._dismiss_no_commanders(shot):
                    return False
                if is_formation_screen(shot) or is_travel_dialog(shot):
                    return True
            logger.warning("Табличка мира есть, план не открылся — закрываю табличку")
            self._deselect_via_safe_grass()
            return False
        self.tap_rel("start_attack_confirm")
        opened = self._wait_for(
            lambda img: is_formation_screen(img)
            or is_no_commanders_parchment(img)
            or (kind in {"samurai", "nomad"} and is_difficulty_dialog(img)),
            timeout=10,
            label="формирование",
        )
        shot = opened if opened is not None else self._image()
        if self._dismiss_no_commanders(shot):
            return False
        if kind in {"samurai", "nomad"} and is_difficulty_dialog(shot):
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
        if before[1] > 0 and before[0] >= before[1]:
            logger.info(
                "Пикер уже {}/{} — карту другого солдата не жму",
                before[0],
                before[1],
            )
            self._last_picker_fill = before
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
        if bool(getattr(self, "_picker_max_adjusted", False)):
            confirm = find_picker_confirm_button(image)
            if confirm is not None:
                logger.warning(
                    "PACK: MAX уже был — жму галочку ({:.3f},{:.3f})",
                    confirm[0],
                    confirm[1],
                )
                self._tap_norm_exact(*confirm)
                CONTROL.sleep(0.22)
                after = self._image()
                occupancy = self._read_ratio_from_image(
                    after, "picker_units"
                ) or self._read_ratio_from_image(after, "formation_units")
                if self._positive_unit_fill(occupancy):
                    self._last_picker_fill = occupancy
                else:
                    self._last_picker_fill = self._assume_picker_capacity(
                        after, self._last_picker_fill
                    )
                return True
            logger.info("Пикер fallback: adjustment уже выполнен — второй MAX/minus запрещён")
            return False
        logger.warning("Пикер: OCR застрял — MAX по шаблону/разметке без ожидания 10/10")
        self._picker_max_adjusted = True
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
        if before and before[1] > 0 and before[0] >= before[1]:
            self._last_picker_fill = before
            logger.info("Пикер уже {}/{} — adjustment не жму", before[0], before[1])
            return True
        if bool(getattr(self, "_picker_max_adjusted", False)):
            logger.info("Пикер MAX уже нажат в этом overlay — повтор stale-frame запрещён")
            return False
        point = find_picker_max_control(image)
        self._picker_max_adjusted = True
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
                if cur > 0 and cap > 0 and cur >= cap:
                    self._last_picker_fill = (cur, cap)
                    logger.info(
                        "Пикер: галочка видна после MAX — считаю {}/{}",
                        cur,
                        cap,
                    )
                    return True
                if cur > 0:
                    self._last_picker_fill = (cur, cap)
                    logger.info(
                        "Пикер: галочка при {}/{} — MAX не добрал, слот не полный",
                        cur,
                        cap,
                    )
                    return False
            if time.time() >= next_heartbeat:
                logger.info("Пикер: жду MAX… ({:.0f}s)", time.time() - started)
                next_heartbeat = time.time() + 2.0
            CONTROL.sleep(0.25)
        if latest:
            self._last_picker_fill = latest
        return bool(latest and latest[0] >= latest[1] > 0)

    def _fill_picker_to_capacity(self) -> bool:
        """Select a unit row if empty, then MAX until current==capacity."""
        live = self._read_ratio_from_image(self._image(), "picker_units")
        if live and live[1] > 0 and live[0] >= live[1]:
            self._last_picker_fill = live
            logger.info(
                "Пикер уже {}/{} — другого солдата не выбираю, только MAX/галочка",
                live[0],
                live[1],
            )
        elif not self._positive_unit_fill(self._last_picker_fill):
            self._select_best_picker_card()
        max_attempts = int((self.config.get("vision") or {}).get("picker_max_attempts") or 3)
        wait_seconds = self._vision_seconds("picker_fill_timeout_seconds", 10)
        seen: list[tuple[int, int]] = []
        for attempt in range(max(1, max_attempts)):
            if self._dump_picker_max():
                return True
            live = self._read_ratio_from_image(self._image(), "picker_units")
            if live and live[1] > 0 and live[0] >= live[1]:
                self._last_picker_fill = live
                return True
            if self._positive_unit_fill(live):
                if live in seen or (seen and live[0] < seen[-1][0]):
                    logger.warning(
                        "Пикер без прогресса/осцилляция {} -> {} — не меняю тип в занятом слоте",
                        seen[-1] if seen else None,
                        live,
                    )
                    self._last_picker_fill = live
                    return False
                seen.append(live)
                self._last_picker_fill = live
            if not self._positive_unit_fill(self._last_picker_fill):
                self._select_best_picker_card()
            elif (
                self._last_picker_fill
                and self._last_picker_fill[1] > 0
                and self._last_picker_fill[0] < self._last_picker_fill[1]
            ):
                logger.info(
                    "Пикер {}/{} после MAX — сохраняю стек; другой тип в этом слоте не выбираю",
                    self._last_picker_fill[0],
                    self._last_picker_fill[1],
                )
                return False
            if attempt + 1 < max_attempts:
                logger.info("MAX без заполнения — повтор {}/{}", attempt + 2, max_attempts)
                CONTROL.sleep(0.35)
        if self._positive_unit_fill(self._last_picker_fill):
            return False
        if self._vision_fill_picker_fallback():
            return True
        live = self._read_ratio_from_image(self._image(), "picker_units")
        if live and live[1] > 0 and live[0] >= live[1]:
            self._last_picker_fill = live
            return True
        if self._positive_unit_fill(self._last_picker_fill) and self._last_picker_fill[0] >= self._last_picker_fill[1]:
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

    def _fill_residual_unit_slots(
        self,
        formation: Any,
        initial: tuple[int, int],
    ) -> tuple[bool, str, Any]:
        """Preserve a partial first stack, then fill adjacent empty slots left-to-right."""
        current = initial
        slots = [
            key
            for key in ("unit_slot_second", "unit_slot_third", "unit_slot_fourth")
            if key in (self.layout.get("buttons") or {})
        ]
        if not slots:
            logger.warning("Формирование {}/{}: соседнего пустого слота нет", *current)
            return False, "residual_slot_not_found", formation
        seen_totals = {current}
        for slot_key in slots:
            if current[1] > 0 and current[0] >= current[1]:
                return True, "", formation
            logger.info(
                "Сохраняю занятый слот {}/{}; открываю следующий пустой {} для остатка {}",
                current[0],
                current[1],
                slot_key,
                max(0, current[1] - current[0]),
            )
            picker, reason = self._open_unit_picker(slot_key)
            if picker is None:
                return False, reason or "residual_picker_not_found", formation
            self._last_picker_fill = None
            if not self._fill_picker_to_capacity() and not self._positive_unit_fill(self._last_picker_fill):
                return False, "residual_units_unavailable", formation
            observed = self._last_picker_fill
            confirmed, reason, latest = self.diagnose_unit_picker_confirm(
                click=True,
                observed_fill=observed,
            )
            if not confirmed or latest is None:
                return False, reason or "residual_confirm_failed", formation
            formation = latest
            total = self._read_ratio_from_image(formation, "formation_units")
            if not total:
                return False, "center_capacity_not_read", formation
            if total in seen_totals or total[0] <= current[0]:
                logger.warning(
                    "Добавочный слот не увеличил итог {} -> {} — стоп осцилляции",
                    current,
                    total,
                )
                return False, "unit_picker_oscillation", formation
            if total[1] != current[1] or total[0] < current[0]:
                logger.warning("Добавочный слот заменил прежний стек {} -> {} — не продолжаю", current, total)
                return False, "unit_stack_replaced", formation
            logger.info(
                "Добавочный слот подтверждён: первый стек сохранён, итог {}/{}",
                total[0],
                total[1],
            )
            seen_totals.add(total)
            current = total
        if current[1] > 0 and current[0] >= current[1]:
            return True, "", formation
        return False, "residual_units_unavailable", formation

    def _prepare_single_center_wave(self) -> tuple[bool, str]:
        # Every attack, including the 4th+: cell → MAX → 10/10 → green check.
        # After OK the formation stays confirmed — never pick a different soldier.
        if self._speed_burst_active():
            shot = None
            units = None
            try:
                shot = self._image()
                units = self._read_ratio_from_image(shot, "formation_units")
            except Exception:
                pass
            empty_seen = bool(getattr(self, "_pack_empty_wave_just_seen", False))
            if empty_seen:
                self._pack_empty_wave_just_seen = False
                logger.warning("PACK: игра сказала волна пустая — игнорирую OCR {} и добираю", units)
            elif units and units[0] > 0:
                logger.warning(
                    "PACK: волна {}/{} уже есть — отправляю без добора",
                    units[0],
                    units[1],
                )
                self._last_picker_fill = units
                return True, ""
            if shot is not None and find_picker_cards(shot):
                confirm = find_picker_confirm_button(shot)
                if confirm is not None and _center_parchment_ratio(shot) > 0.28:
                    logger.warning(
                        "PACK: пикер открыт, OCR пуст — жму галочку ({:.3f},{:.3f})",
                        confirm[0],
                        confirm[1],
                    )
                    self._tap_norm_exact(*confirm)
                    CONTROL.sleep(0.22)
                    self._last_picker_fill = (1, 1)
                    return True, ""
        last_reason = ""
        attempts = 1 if self._speed_burst_active() else 2
        for sequence_attempt in range(attempts):
            if not self._speed_burst_active():
                self._dismiss_empty_wave_warning()
            image = self._image()
            overlay_open = self._picker_overlay_open(image)
            picker_ratio = self._read_ratio_from_image(image, "picker_units")
            formation_units = self._read_ratio_from_image(image, "formation_units")
            picker_full = bool(
                picker_ratio and picker_ratio[1] > 0 and picker_ratio[0] >= picker_ratio[1]
            )
            wave_assigned = bool(formation_units and formation_units[0] > 0)
            if sequence_attempt:
                if picker_full or (not overlay_open and wave_assigned):
                    logger.warning(
                        "Пикер: повтор 2/2 не выбирает другого солдата — уже {}",
                        picker_ratio or formation_units,
                    )
                    if overlay_open and picker_full:
                        self._last_picker_fill = picker_ratio
                    elif wave_assigned:
                        self._last_picker_fill = formation_units
                else:
                    logger.warning("Пикер: повтор полной последовательности 2/2")
                    if not overlay_open:
                        slot = self.layout.get("buttons", {}).get("unit_slot")
                        if slot:
                            self._tap_norm_exact(float(slot[0]), float(slot[1]))
                            self._speed_sleep(0.5)
                    if not self._positive_unit_fill(self._last_picker_fill):
                        self._last_picker_fill = None
            elif not overlay_open and wave_assigned:
                full = bool(
                    formation_units
                    and formation_units[1] > 0
                    and formation_units[0] >= formation_units[1]
                )
                if full:
                    logger.info(
                        "Волна уже {}/{} — пикер не открываю, иду в Нападение",
                        formation_units[0],
                        formation_units[1],
                    )
                    self._last_picker_fill = formation_units
                else:
                    logger.info(
                        "Волна {}/{} не 100% — открою пикер добрать, другого солдата не выбираю",
                        formation_units[0],
                        formation_units[1],
                    )
                    slot = self.layout.get("buttons", {}).get("unit_slot")
                    if slot:
                        self._tap_norm_exact(float(slot[0]), float(slot[1]))
                        self._speed_sleep(0.5)
                    self._last_picker_fill = formation_units
            elif picker_full:
                self._last_picker_fill = picker_ratio
            else:
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
        if not self._picker_overlay_open(formation):
            units = self._read_ratio_from_image(formation, "formation_units")
            if units and units[1] > 0 and units[0] >= units[1]:
                logger.info(
                    "Формирование уже {}/{} — второй солдат не выбираю",
                    units[0],
                    units[1],
                )
                self._last_picker_fill = units
                observed = units
                final_units = units
                final_tools = self._read_ratio_from_image(formation, "formation_tools")
                if final_tools is None:
                    final_tools = (0, 0)
                if not final_units or final_units[1] <= 0:
                    save_shot(formation, "formation-verification-failed.png")
                    return False, "center_capacity_not_read"
                if not final_tools or final_tools[0] != 0:
                    save_shot(formation, "formation-verification-failed.png")
                    return False, "tools_not_empty"
                fill_ratio = final_units[0] / final_units[1]
                kind = str(
                    getattr(self, "_wave_kind", "")
                    or (self.config or {}).get("current_target_kind")
                    or ""
                )
                minimum = self._kind_min_fill(kind)
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
            if units and units[0] > 0:
                logger.info(
                    "Формирование {}/{} неполное — открываю пикер добрать, другого солдата не выбираю",
                    units[0],
                    units[1],
                )
                self._last_picker_fill = units
        slot_keys = ("unit_slot",)
        for slot_key in slot_keys:
            picker, reason = self._open_unit_picker(slot_key)
            if picker is None:
                return False, reason
            picker_ratio = self._read_ratio_from_image(picker, "picker_units")
            if (not picker_ratio or picker_ratio[1] <= 0) and self._picker_overlay_open(picker):
                picker_ratio = (0, 10)
            if not picker_ratio or picker_ratio[1] <= 0:
                return False, "center_capacity_not_read"
            picker_full = self._fill_picker_to_capacity()
            if not self._positive_unit_fill(self._last_picker_fill):
                shot = self._image()
                occupancy = self._read_ratio_from_image(
                    shot, "picker_units"
                ) or self._read_ratio_from_image(shot, "formation_units")
                if self._positive_unit_fill(occupancy):
                    logger.warning(
                        "PACK: last fill пустой, на экране {}/{} — набор есть, не abort",
                        occupancy[0],
                        occupancy[1],
                    )
                    self._last_picker_fill = occupancy
                else:
                    save_shot(shot, "unit-picker-selection-no-progress.png")
                    return False, "unit_picker_fill_not_retained"
            confirmed, reason, formation = self.diagnose_unit_picker_confirm(
                click=True,
                observed_fill=self._last_picker_fill,
            )
            if confirmed and formation is not None and not picker_full:
                total = self._read_ratio_from_image(formation, "formation_units")
                if not total:
                    total = self._last_picker_fill
                if total and total[1] > 0 and total[0] < total[1]:
                    logger.info(
                        "Частичный стек {}/{} подтверждён и сохранён — добираю в соседнем слоте",
                        total[0],
                        total[1],
                    )
                    additive_ok, additive_reason, formation = self._fill_residual_unit_slots(
                        formation,
                        total,
                    )
                    if not additive_ok:
                        save_shot(formation, "unit-picker-residual-no-progress.png")
                        return False, additive_reason
                    self._last_picker_fill = self._read_ratio_from_image(
                        formation, "formation_units"
                    ) or total
                    break
            if confirmed and formation is not None:
                break
            latest = self._image()
            picker_closed = not self._picker_overlay_open(latest)
            if self._positive_unit_fill(self._last_picker_fill) and picker_closed:
                logger.info(
                    "OK закрыл пикер при {}/{} — другого солдата не выбираю, иду в Нападение",
                    self._last_picker_fill[0],
                    self._last_picker_fill[1],
                )
                formation = latest
                break
            return False, reason
        observed = self._last_picker_fill
        final_units = self._read_ratio_from_image(formation, "formation_units")
        if final_units is not None and final_units[0] <= 0:
            if self._positive_unit_fill(observed):
                logger.info(
                    "OCR формирования {}/{} после пикера {}/{} — беру заполнение пикера, Нападение",
                    final_units[0],
                    final_units[1],
                    observed[0],
                    observed[1],
                )
                final_units = observed
            else:
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
        kind = str(getattr(self, "_wave_kind", "") or (self.config or {}).get("current_target_kind") or "")
        minimum = self._kind_min_fill(kind)
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

    def _return_home_via_castle_hud(self) -> bool:
        """Always recenter through the canonical topmost navigation-list row."""
        if not getattr(self, "adb", None):
            return False
        kind = str(self.config.get("current_target_kind") or "")
        spec = spec_for_kind(kind)
        world_id = str(getattr(self, "_switched_world_id", None) or (spec.id if spec else ""))
        if not world_id:
            return False
        image = self._image()
        if self._plan_or_picker_open(image) or is_travel_dialog(image) or is_formation_screen(image):
            image = self._await_world_map(timeout=6) or image
        if self._plan_or_picker_open(image) or is_travel_dialog(image) or is_formation_screen(image):
            logger.info("Canonical home ждёт завершения открытой формации/похода")
            return False
        logger.info(
            "После атаки: HUD не жму; открываю Навигация → Выбери место → topmost row world={}",
            world_id,
        )
        if not self._navigation_home_for_world(world_id):
            return False
        return self._finish_verified_home()

    @staticmethod
    def _castle_identity_matches(expected: dict[str, Any], observed: dict[str, Any]) -> bool:
        """Diagnostic-only coordinate comparison; account-specific names are ignored."""
        expected_coords = expected.get("coords")
        observed_coords = observed.get("coords")
        return bool(
            isinstance(expected_coords, (list, tuple))
            and len(expected_coords) == 2
            and isinstance(observed_coords, (list, tuple))
            and len(observed_coords) == 2
            and tuple(map(int, expected_coords)) == tuple(map(int, observed_coords))
        )

    @staticmethod
    def _normalize_castle_name(value: str) -> str:
        clean = compact_ui(value)
        for wrong in ("3amokk", "3amok", "zamok"):
            clean = clean.replace(wrong, "замок")
        return clean

    def _read_castle_identity(self, image: Any, *, banner: bool) -> dict[str, Any]:
        region = [0.08, 0.00, 0.92, 0.22] if banner else [0.18, 0.00, 0.82, 0.10]
        text = ocr_text_ui(crop_rel(image, region), psm=6)
        coord_match = re.search(
            r"[XХ]\s*[:=]\s*(\d{1,4})\s*[/\\|]?\s*[YУ]\s*[:=]\s*(\d{1,4})",
            text,
            flags=re.IGNORECASE,
        )
        coords = (int(coord_match.group(1)), int(coord_match.group(2))) if coord_match else None
        name = ""
        for line in text.splitlines():
            normalized = self._normalize_castle_name(line)
            if any(token in normalized for token in ("замок", "castle")):
                name = re.sub(r"[XХ]\s*[:=].*$", "", line, flags=re.IGNORECASE).strip(" |._-")
                break
        return {"name": name, "coords": list(coords) if coords else None, "ocr": text[:180]}

    def _deselect_via_safe_grass(self) -> bool:
        """Tap empty grass/water so a selected castle does not block pan/hunt."""
        if self._speed_burst_active():
            self._tap_norm_exact(0.24, 0.70)
            CONTROL.sleep(0.05)
            return True
        latest = self._image()
        candidates = (
            (0.16, 0.54),
            (0.84, 0.54),
            (0.24, 0.70),
            (0.76, 0.70),
            (0.18, 0.34),
            (0.82, 0.34),
        )
        ranked: list[tuple[float, tuple[float, float]]] = []
        width, height = latest.size
        for point in candidates:
            px, py = int(point[0] * width), int(point[1] * height)
            radius = max(10, int(min(width, height) * 0.025))
            patch = latest.crop((px - radius, py - radius, px + radius, py + radius)).convert("L")
            ranked.append((float(ImageStat.Stat(patch).stddev[0]), point))
        for texture, point in sorted(ranked):
            if texture > 34.0:
                continue
            logger.info(
                "deselect: безопасная свободная область ({:.3f}, {:.3f}) texture={:.1f}",
                point[0],
                point[1],
                texture,
            )
            self._tap_norm_exact(*point)
            CONTROL.sleep(0.35)
            plain = self._image()
            if (
                is_map_screen(plain)
                and not is_info_plaque(plain)
                and not is_overview_plaque(plain)
                and find_target_attack_button(plain) is None
                and not self._plan_or_picker_open(plain)
                and not is_travel_dialog(plain)
            ):
                return True
        return False

    def _finish_verified_home(self) -> bool:
        latest = self._image()
        main, viewport = self._read_map_coords(latest)
        marker = find_main_castle_marker(latest)
        centered = bool(
            marker
            or (
                main is not None
                and viewport is not None
                and abs(main[0] - viewport[0]) <= 6
                and abs(main[1] - viewport[1]) <= 6
            )
        )
        if not centered:
            logger.warning("Домой нажато, но центральный замок не подтверждён — pending сохраняю")
            return False
        started = time.monotonic()
        if not self._deselect_via_safe_grass():
            logger.warning("Не нашёл подтверждённую свободную область — поиск не начинаю")
            return False
        logger.info(
            "Домой → plain map подтверждён за {:.2f}с; pan/search разрешён",
            time.monotonic() - started,
        )
        self._hunt_queue = []
        self._blocked_screen_targets = []
        self.store.live.post_attack_home_pending = False
        self.store.live.last_action = "домой после атаки · центральный замок подтверждён"
        self.store.save()
        logger.info("Дома подтверждено — pending снят; новая охота с ближайшей небитой цели")
        return True

    def _open_messages(self) -> bool:
        """Reveal and open Messages without tapping an unverified footer button."""
        image = self._image()
        if self._plan_or_picker_open(image):
            self._report_check_queued = True
            return False
        if is_inbox_screen(image):
            return True
        point = find_messages_button(image)
        for attempt in range(6):
            if point is not None:
                break
            logger.info(
                "Сообщения скрыты — короткий drag нижней панели справа налево ({}/6)",
                attempt + 1,
            )
            self._swipe_norm((0.78, 0.94), (0.58, 0.94))
            image = self._image()
            point = find_messages_button(image)
        if point is None:
            logger.warning("После drag надпись «Сообщения» не найдена — ничего не нажимаю")
            return False
        logger.info("Открываю подтверждённую OCR кнопку «Сообщения» ({:.3f}, {:.3f})", *point)
        self._tap_norm_exact(*point)
        CONTROL.sleep(0.6)
        return is_inbox_screen(self._image())

    def process_unread_battle_reports(self, world_hint: str = "") -> int:
        """Process green unread battle reports between attacks; never interrupt formation."""
        if self._plan_or_picker_open():
            self._report_check_queued = True
            return 0
        if not self._open_messages():
            self._last_report_check = time.time()
            self._report_check_queued = False
            return 0
        processed = 0
        for _ in range(30):
            inbox = self._image()
            rows = unread_battle_rows(inbox)
            if not rows:
                break
            x, y, subject, stamp = rows[0]
            logger.info("Открываю зелёный боевой отчёт: {}", subject)
            self._tap_norm_exact(x, y)
            CONTROL.sleep(0.55)
            detail = self._image()
            if not is_victory_report(detail):
                logger.warning("Строка не открылась как «Победа» — закрываю отчёт, данные не придумываю")
            else:
                shot_path = save_shot(detail, f"farm_report_{int(time.time() * 1000)}.png")
                report = parse_victory_report(
                    detail,
                    world=world_hint,
                    subject=subject,
                    report_timestamp=stamp,
                    screenshot=str(shot_path),
                )
                if self._farm_ledger.append(report):
                    processed += 1
                    summary = self._farm_ledger.summary()
                    self.telegram.report_farm_report(report.to_dict())
                    self.store.live.farm_summary = summary
                    self.store.live.reports_processed = int(summary.get("reports") or 0)
                    self.store.save()
            # Victory report's lower-right green check is the documented close/next control.
            self._tap_forced(0.80, 0.93)
            CONTROL.sleep(0.5)
            if not is_inbox_screen(self._image()):
                break
        self._last_report_check = time.time()
        self._report_check_queued = False
        summary = self._farm_ledger.write_summary()
        write_progress(
            reports_processed=int(summary.get("reports") or 0),
            farm_summary=summary,
            telegram_available=bool(self.telegram.ready),
        )
        logger.info("Проверка почты завершена: новых боевых отчётов {}", processed)
        self._dismiss_inbox_if_open()
        return processed

    def delete_all_messages(self) -> bool:
        """Final-cleanup only: process reports, then Delete all -> GREEN confirmation."""
        self.process_unread_battle_reports()
        if not self._open_messages():
            return False
        image = self._image()
        text = ocr_text_ui(crop_rel(image, [0.55, 0.10, 0.94, 0.34]), psm=6).lower()
        if "удалить" not in text and "delete" not in text:
            logger.warning("«Удалить все» OCR не подтверждено — очистку не выполняю")
            return False
        self._tap_norm_exact(0.80, 0.22)
        CONTROL.sleep(0.45)
        confirm = self._image()
        prompt = ocr_text_ui(crop_rel(confirm, [0.12, 0.10, 0.88, 0.55]), psm=6).lower()
        if "удал" not in prompt and "delete" not in prompt:
            logger.warning("Диалог удаления не подтверждён OCR — зелёную печать не жму")
            return False
        logger.warning("Финальная очистка: «Удалить все» — жму только ЗЕЛЁНУЮ галочку")
        self._tap_forced(0.80, 0.86)
        CONTROL.sleep(0.7)
        cleared = not unread_battle_rows(self._image())
        write_progress(inbox_cleared=cleared)
        return cleared

    def _home_after_send(self, kind: str, result: str) -> None:
        if result != kind:
            return
        if self._speed_burst_active():
            return
        self._return_home_via_castle_hud()

    def _ensure_post_attack_home(self) -> bool:
        live = getattr(getattr(self, "store", None), "live", None)
        if getattr(live, "post_attack_home_pending", False) is not True:
            return True
        logger.warning("Перед новой охотой выполняю сохранённый post_attack_home_pending")
        return self._return_home_via_castle_hud()

    def _read_ratio_from_image(self, image: Any, key: str) -> tuple[int, int] | None:
        region = self.layout.get("regions", {}).get(key)
        return parse_ratio(ocr_text(crop_rel(image, region))) if region else None

    def _movement_option(self, image: Any) -> tuple[str, int | None]:
        if self._speed_burst_active():
            self.tap_rel("feather_option")
            return "feather", 1
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

    def _pack_hud_matches_world(self, image: Any, world_id: str) -> bool:
        """Fast terrain/HUD gate. Do not hunt Great Empire grass on another world's pack."""
        if (
            is_travel_dialog(image)
            or is_formation_screen(image)
            or self._picker_overlay_open(image)
        ):
            return False
        if is_loading_screen(image) or is_world_list_open(image) or is_choose_place_screen(image):
            return False
        if not is_map_screen(image):
            return False
        grass = map_grass_ratio(image)
        if world_id == "great_empire":
            return grass >= 0.12
        if grass >= 0.12:
            logger.warning("PACK HUD: зелёная империя, нужен {} — не охочусь", world_id)
            return False
        observed = getattr(self, "_pack_hud_observed", None)
        if observed is not None and observed != world_id:
            logger.warning("PACK HUD: мир {} а нужен {} — не охочусь", observed, world_id)
            return False
        return True

    def _log_pack_hud(self, image: Any, world_id: str) -> None:
        grass = map_grass_ratio(image)
        if self._speed_burst_active():
            if world_id == "great_empire":
                self._pack_hud_observed = "great_empire" if grass >= 0.12 else None
            else:
                self._pack_hud_observed = None if grass >= 0.12 else world_id
            logger.warning(
                "PACK HUD after jump want={} grass={:.3f} (OCR skipped)",
                world_id,
                grass,
            )
            return
        try:
            hud = self._read_castle_identity(image, banner=True)
        except Exception:
            hud = {}
        observed = match_world_id(f"{hud.get('ocr') or ''} {hud.get('name') or ''}")
        self._pack_hud_observed = observed
        logger.warning(
            "PACK HUD after jump want={} got={} grass={:.3f} coords={}",
            world_id,
            observed,
            grass,
            hud.get("coords"),
        )

    def _pack_confirm_open_travel(self, kind: str) -> str:
        """Finish a leftover movement-planning dialog instead of hunting behind it."""
        travel = self._image()
        if not is_travel_dialog(travel):
            return "travel_dialog_not_found"
        if self._dismiss_no_commanders(travel):
            return "no_commanders"
        self._mark_pack_prep(kind)
        if self._speed_burst_active():
            movement = "gold"
        else:
            movement, _feathers = self._movement_option(travel)
            if movement == "unknown":
                self.tap_rel("travel_cancel")
                return "feather_count_not_read"
        one_way = self._read_march_time(travel)
        if one_way is None:
            if self._speed_burst_active():
                one_way = 1
            else:
                self.tap_rel("travel_cancel")
                return "march_time_not_read"
        logger.warning("PACK leftover travel — confirm {} one_way={}", kind, one_way)
        fake_target = {
            "kingdom": int((self.config.get("baron_attacks") or {}).get("kingdom", 0)),
            "x": int(
                self._selected_target_coords[0]
                if getattr(self, "_selected_target_coords", None)
                else 500
            ),
            "y": int(
                self._selected_target_coords[1]
                if getattr(self, "_selected_target_coords", None)
                else 500
            ),
        }
        return self._finish_attack(kind, fake_target, one_way, movement)

    def _pack_dismiss_overlay(self, image: Any) -> bool:
        """Close inbox/taxes parchment. Never the nav list. Never ruby/+. Never the map HUD."""
        if is_travel_dialog(image) or is_formation_screen(image) or self._picker_overlay_open(image):
            return False
        if is_map_screen(image) and not is_info_plaque(image) and not is_inbox_screen(image):
            return False
        parchment = _center_parchment_ratio(image)
        if parchment <= 0.40:
            return False
        if self._picker_overlay_open(image) or is_formation_screen(image):
            return False
        if len(find_world_list_sextants(image)) >= 3:
            return False
        now = time.time()
        last = float(getattr(self, "_pack_last_x_at", 0.0) or 0.0)
        if now - last < 1.0:
            CONTROL.sleep(0.12)
            return True
        close = find_parchment_title_close(image)
        if close is None:
            alt = popup_action(image) or find_red_cross_force(image)
            if alt is not None:
                close = (float(alt[0]), float(alt[1]))
        if (
            close is None
            or close[1] > 0.22
            or close[0] < 0.68
            or close[0] >= 0.88
            or is_ruby_plus_hud_point(*close)
            or is_offer_rail_point(*close)
        ):
            logger.warning(
                "PACK overlay parchment={:.2f} — крестик небезопасен {}, не жму ruby/+",
                parchment,
                close,
            )
            return False
        self._pack_last_x_at = now
        logger.warning(
            "PACK overlay parchment={:.2f} — X ({:.3f},{:.3f})",
            parchment,
            close[0],
            close[1],
        )
        self._tap_forced(*close)
        CONTROL.sleep(0.15)
        return True

    def _world_speed_pack(self, kind: str) -> str:
        """5 real sends in this world; wall-clock from first prep to 5th send must be ≤40s."""
        spec = spec_for_kind(kind)
        world_id = spec.id if spec is not None else "great_empire"
        self._begin_world_pack(kind)
        logger.warning(
            "PACK loop {} world={} cycles={}/10",
            kind,
            world_id,
            int(getattr(self, "_qualifying_cycles", 0) or 0),
        )
        last = "no_targets"
        try:
            probe = self._image()
        except Exception:
            probe = None
        if probe is not None and self._pack_hud_matches_world(probe, world_id):
            self._switched_world_id = world_id
        else:
            current = getattr(self, "_switched_world_id", None)
            if current != world_id and (current is not None or world_id != "great_empire"):
                self._fast_world_home(world_id)
                CONTROL.sleep(0.08)
            elif current is None:
                self._switched_world_id = world_id
        attempts = 8 if kind == "storm_fort" else 28
        for _ in range(attempts):
            CONTROL.check()
            if getattr(self, "_pack_finished", False):
                return last if last != "no_targets" else kind
            image = self._image()
            if self._dismiss_empty_wave_warning():
                continue
            if is_travel_dialog(image):
                logger.warning("PACK leftover travel — confirm send")
                result = self._pack_confirm_open_travel(kind)
                last = result
                if result == kind:
                    self._block_screen_target((0.50, 0.50))
                elif result == "no_commanders":
                    return result
                continue
            if self._picker_overlay_open(image) or is_formation_screen(image):
                logger.warning("PACK picker/formation open — execute send")
                result = self._execute_formation_attack(kind, (0.50, 0.50))
                last = result
                if result == kind:
                    self._block_screen_target((0.50, 0.50))
                    leftover = self._image()
                    if is_formation_screen(leftover):
                        self.close_formation_plan()
                elif result == "no_commanders":
                    return result
                continue
            if is_loading_screen(image):
                CONTROL.sleep(0.12)
                continue
            if self._dismiss_special_offers_if_open(image):
                continue
            if is_world_list_open(image) or is_choose_place_screen(image):
                self._tap_open_world_list_row(world_id, image)
                continue
            recent_jump = time.time() - float(
                getattr(self, "_pack_last_list_tap_at", 0.0) or 0.0
            ) < 5.0
            if self._pack_dismiss_overlay(image):
                continue
            if not is_map_screen(image):
                if recent_jump:
                    CONTROL.sleep(0.12)
                    continue
                now = time.time()
                last_home = float(getattr(self, "_pack_last_home_at", 0.0) or 0.0)
                if now - last_home >= 2.5:
                    self._fast_world_home(world_id)
                    self._pack_last_home_at = now
                else:
                    CONTROL.sleep(0.12)
                continue
            if not self._pack_hud_matches_world(image, world_id):
                now = time.time()
                last_home = float(getattr(self, "_pack_last_home_at", 0.0) or 0.0)
                if now - last_home >= 2.0:
                    self._fast_world_home(world_id)
                    self._pack_last_home_at = now
                else:
                    CONTROL.sleep(0.12)
                continue
            if is_info_plaque(image) or is_overview_plaque(image):
                self._deselect_via_safe_grass()
                image = self._image()
            targets = self._list_eligible_targets(image, kind)
            if not targets:
                last = "no_targets"
                if kind == "storm_fort":
                    logger.warning(
                        "PACK: острова ураганов не открыты или нет фортов — цикл без шторма не считаю"
                    )
                    if spec is not None:
                        self.store.skip_mode(spec.mode_id)
                    return "world_skip_empty"
                streak = int(getattr(self, "_pack_no_target_streak", 0) or 0) + 1
                self._pack_no_target_streak = streak
                logger.warning(
                    "PACK: на карте нет {} — streak={} (навигацию не открываю)",
                    kind,
                    streak,
                )
                pans = (
                    ((0.70, 0.48), (0.32, 0.48)),
                    ((0.32, 0.48), (0.70, 0.48)),
                    ((0.50, 0.60), (0.50, 0.34)),
                    ((0.50, 0.34), (0.50, 0.60)),
                )
                start, end = pans[(streak - 1) % 4]
                logger.warning("PACK: пан карты ({:.2f},{:.2f})→({:.2f},{:.2f})", start[0], start[1], end[0], end[1])
                self._swipe_norm(start, end)
                CONTROL.sleep(0.05)
                if streak >= 12:
                    now = time.time()
                    last_home = float(getattr(self, "_pack_last_home_at", 0.0) or 0.0)
                    if now - last_home >= 8.0:
                        self._fast_world_home(world_id)
                        self._pack_last_home_at = now
                    self._pack_no_target_streak = 0
                continue
            self._pack_no_target_streak = 0
            point = targets[0].point
            if not self._open_formation(point, kind):
                self._blocked_screen_targets.append(point)
                last = "popup_not_found"
                continue
            self._mark_pack_prep(kind)
            result = self._execute_formation_attack(kind, point)
            last = result
            if result == kind:
                self._block_screen_target(point)
            elif result == "no_commanders":
                return result
        times = getattr(self, "_pack_send_times", None) or []
        if times:
            span = time.time() - float(self._pack_prep_started_at or times[0])
            logger.warning("PACK floor {} {} sends spanned {:.2f}s", kind, len(times), span)
        return last

    def _baron_speed_burst(self) -> str:
        return self._world_speed_pack("baron")

    def on_screen_attack(self, kind: str) -> str:
        if getattr(self, "_hunt_queue", None) is None:
            self._hunt_queue = []
        if getattr(self, "_blocked_screen_targets", None) is None:
            self._blocked_screen_targets = []
        if kind == "baron" and self._speed_burst_active():
            return self._baron_speed_burst()
        if is_world_npc_kind(kind) and self._speed_burst_active():
            return self._world_speed_pack(kind)
        if not self._ensure_post_attack_home():
            return "post_attack_home_pending"
        try:
            already = self._image()
        except Exception:
            already = None
        if already is not None and self._plan_or_picker_open(already):
            if getattr(self, "_need_ge_home", False):
                logger.info(
                    "Набор армии открыт, но старт с Великой империи — закрываю старый план и иду в империю"
                )
                self.close_formation_plan()
            else:
                logger.info(
                    "Экран планирования уже открыт — не закрываю и не меняю мир, продолжаю набор/Нападение"
                )
                result = self._execute_formation_attack(kind, (0.50, 0.50))
                self._home_after_send(kind, result)
                return result
        if kind == "baron":
            try:
                probe = self._image()
            except Exception:
                probe = None
            if probe is not None:
                self._dismiss_inbox_if_open(probe)
                probe = self._image()
                if (
                    not is_map_screen(probe)
                    or is_inbox_screen(probe)
                    or map_grass_ratio(probe) < 0.12
                ):
                    logger.info("Бароны: не Великая империя / оверлей — принудительно Навигация домой")
                    self._need_ge_home = True
                    self._switched_world_id = None
        switched = self._ensure_world(kind)
        if switched != "ok":
            if is_world_npc_kind(kind) or kind == "baron":
                logger.warning("Переход в мир не удался: {}", switched)
                return switched
        current = self._image()
        if self._dismiss_no_commanders(current):
            return "no_commanders"
        self._dismiss_empty_wave_warning()
        current = self._image()
        if self._plan_or_picker_open(current):
            logger.info(
                "Экран планирования уже открыт — не закрываю, продолжаю набор/Нападение"
            )
            result = self._execute_formation_attack(kind, (0.50, 0.50))
            self._home_after_send(kind, result)
            return result
        self._dismiss_quit_game_if_open(current)
        self._dismiss_special_offers_if_open(current)
        if not self._hunt_queue:
            self._blocked_screen_targets.clear()
            self._hunt_queue = self._collect_hunt_batch(kind)
            if not self._hunt_queue and is_world_npc_kind(kind):
                tries = int(getattr(self, "_world_recenter_tries", 0) or 0)
                if tries < 3:
                    self._world_recenter_tries = tries + 1
                    if self._recenter_current_world_via_nav(kind):
                        self._hunt_queue = self._collect_hunt_batch(kind)
            if not self._hunt_queue:
                logger.info("На карте нет доступных целей этого мира")
                if is_world_npc_kind(kind):
                    return self._handle_world_no_targets(kind)
                return "no_targets"
            self._world_recenter_tries = 0
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
                result = self._execute_formation_attack(kind, (0.50, 0.50))
                self._home_after_send(kind, result)
                return result
            self._dismiss_quit_game_if_open(current)
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
                if is_world_npc_kind(kind):
                    logger.info("Цель мира не дала план — следующая из пачки без трёх повторов")
                    break
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
            result = self._execute_formation_attack(kind, point)
            self._home_after_send(kind, result)
            return result
        if is_world_npc_kind(kind) and int(getattr(self, "_world_recenter_tries", 0) or 0) < 3:
            self._world_recenter_tries = int(getattr(self, "_world_recenter_tries", 0) or 0) + 1
            if self._recenter_current_world_via_nav(kind):
                return self.on_screen_attack(kind)
        if is_world_npc_kind(kind):
            return self._handle_world_no_targets(kind)
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
        if not self._speed_burst_active():
            self._dismiss_empty_wave_warning()
        self._last_picker_fill = None
        if is_world_npc_kind(kind):
            ok, reason = self._prepare_waves_for_kind(kind)
        else:
            ok, reason = self._prepare_single_center_wave()
        if not ok:
            if is_world_npc_kind(kind):
                return self._handle_world_fill_failure(kind, reason)
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
        self._mark_pack_prep(kind)
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
        time.sleep(0.05 if self._speed_burst_active() else 0.5)
        travel = self._image()
        one_way = self._read_march_time(travel)
        if one_way is None:
            if self._speed_burst_active():
                one_way = 1
            else:
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
        commander_no = None if self._speed_burst_active() else parse_commander_number(self.read_region("commander_number"))
        if commander_no is None:
            commander_no = self._next_commander
        self._next_commander = int(commander_no) + 1
        one_way = one_way or parse_march_seconds(self.read_region("march_time"))
        if one_way is None:
            self.tap_rel("travel_cancel")
            return "march_time_not_read"
        shot = None if self._speed_burst_active() else capture_game_image(self.config, self.adb)
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
        if not self._speed_burst_active():
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
        if not self._speed_burst_active():
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
        now = time.time()
        times = getattr(self, "_burst_send_times", None)
        if times is None:
            self._burst_send_times = []
            times = self._burst_send_times
        times.append(now)
        window = times[-5:] if len(times) >= 5 else times
        span = window[-1] - window[0] if window else 0.0
        logger.warning(
            "SPEED attack.sent #{} kind={} last{}={:.2f}s (need 5 in 40s pack)",
            len(times),
            kind,
            len(window),
            span,
        )
        if self._speed_burst_active():
            self._note_pack_send(kind)
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
                result = self.search_and_attack(kind, target)
                if result == "stop":
                    return "stop"
                sent += 1
            return f"client:{sent}"

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
