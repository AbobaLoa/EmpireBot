from __future__ import annotations

import re
import time
from typing import Any

from loguru import logger

from e4kbot.bluestacks import save_shot
from e4kbot.control import CONTROL
from e4kbot.vision import (
    find_add_wave_control,
    find_castle_name_hud_point,
    find_navigation_button,
    find_parchment_title_close,
    find_plaque_attack_button,
    find_world_parchment_attack_button,
    find_select_place_button,
    find_world_list_sextants,
    parse_navigation_rows,
    is_choose_place_screen,
    is_formation_screen,
    is_info_plaque,
    is_inbox_screen,
    is_loading_screen,
    is_map_screen,
    is_navigation_submenu,
    map_grass_ratio,
    is_place_list_exhausted,
    is_ruby_plus_hud_point,
    is_select_place_point,
    is_special_offers_screen,
    is_travel_dialog,
    is_world_list_open,
    ocr_text_ui,
    parse_navigation_rows,
    crop_rel,
    SELECT_PLACE_FALLBACK,
)
from e4kbot.worlds import (
    CONTINUE_ONE_WORLD_LINE,
    STORM_UNOPENED_LINE,
    TOUR_WORLDS,
    WORLD_BY_ID,
    NavRow,
    NavigationScan,
    compact_ui,
    is_world_npc_kind,
    looks_like_wrong_world_target,
    match_target_kind,
    scan_from_blob,
    spec_for_kind,
    world_fill_decision,
    write_world_attack_report,
)


FAST_WORLD_SEXTANTS = {
    "great_empire": (0.199, 0.239),
    "everwinter": (0.199, 0.343),
    "burning_sands": (0.199, 0.406),
    "fire_peaks": (0.199, 0.468),
    "storm_islands": (0.199, 0.531),
}


class WorldSwitchMixin:
    """Navigation parchment world travel + 100% wave policy for other kingdoms."""

    def _kind_spec(self, kind: str):
        return spec_for_kind(kind)

    def _kind_min_fill(self, kind: str) -> float:
        spec = spec_for_kind(kind)
        if spec is None:
            return float((self.config.get("vision") or {}).get("minimum_flank_fill") or 0.70)
        return float(spec.fill_ratio)

    @staticmethod
    def _identity_name(blob: str) -> str:
        for line in str(blob or "").splitlines():
            clean = re.sub(r"\b[XYХУ]\s*[:=]\s*\d+.*$", "", line, flags=re.IGNORECASE).strip()
            if clean and any(token in compact_ui(clean) for token in ("замок", "castle", "аванпост")):
                return clean[:80]
        return str(blob or "").splitlines()[0][:80] if blob else ""

    def _configured_central(self, world_id: str) -> dict[str, Any]:
        persisted = dict((getattr(self.store.live, "central_castles", None) or {}).get(world_id) or {})
        configured = dict(((self.config.get("central_castles") or {}).get(world_id)) or {})
        return {**persisted, **configured}

    @staticmethod
    def _blob_coords(blob: str) -> list[tuple[int, int]]:
        return [
            (int(x), int(y))
            for x, y in re.findall(
                r"[XХ]\s*[:=]\s*(\d{1,4})\s*[/\\|]?\s*[YУ]\s*[:=]\s*(\d{1,4})",
                str(blob or ""),
                flags=re.IGNORECASE,
            )
        ]

    def _central_row_for_world(self, scan: NavigationScan, world_id: str) -> NavRow | None:
        rows = (
            list(scan.rows)
            if world_id == "great_empire"
            else [row for row in scan.rows if row.world_id == world_id]
        )
        if not rows:
            return None
        # Account names and coordinates vary. List geometry is authoritative:
        # the canonical/main castle is always the topmost row for this world.
        chosen = min(rows, key=lambda row: float(row.sextant[1]))
        row_index = scan.rows.index(chosen)
        logger.info(
            "Canonical row: world={} index={} y={:.3f} (нижние дубликаты не жму)",
            world_id,
            row_index,
            chosen.sextant[1],
        )
        return chosen

    def _register_central_castles(self, scan: NavigationScan) -> None:
        registry = dict(getattr(self.store.live, "central_castles", None) or {})
        changed = False
        for world_id in WORLD_BY_ID:
            row = self._central_row_for_world(scan, world_id)
            if row is None:
                continue
            configured = dict(((self.config.get("central_castles") or {}).get(world_id)) or {})
            configured_coords = configured.get("coords")
            if row.coords is None and not (
                isinstance(configured_coords, (list, tuple)) and len(configured_coords) == 2
            ):
                continue
            coords = (
                [int(configured_coords[0]), int(configured_coords[1])]
                if isinstance(configured_coords, (list, tuple)) and len(configured_coords) == 2
                else [int(row.coords[0]), int(row.coords[1])]
            )
            value = {
                "world_id": world_id,
                "world": row.world_ru,
                "name": str(configured.get("name") or self._identity_name(row.blob)),
                "coords": coords,
                "row": {
                    "castle": row.castle,
                    "sextant": [float(row.sextant[0]), float(row.sextant[1])],
                },
                "row_blob": row.blob[:300],
            }
            if registry.get(world_id) != value:
                registry[world_id] = value
                changed = True
                logger.info(
                    "Реестр центра {}: {} X:{} Y:{}",
                    world_id,
                    value["name"],
                    value["coords"][0],
                    value["coords"][1],
                )
        if changed:
            self.store.live.central_castles = registry
            self.store.save()

    def _world_list_expected_sextant(self, world_id: str) -> tuple[float, float]:
        nx, ny = FAST_WORLD_SEXTANTS.get(world_id, (0.199, 0.239))
        return nx, ny

    def _tap_open_world_list_row(self, world_id: str, image: Any | None = None) -> bool:
        """Tap the OCR-matched left sextant. Never GE outpost, grass-on-list, or Berimond."""
        try:
            shot = image if image is not None else self._image()
        except Exception:
            return False
        points = find_world_list_sextants(shot)
        if len(points) < 3 and not is_choose_place_screen(shot) and not is_world_list_open(shot):
            return False
        if not is_world_list_open(shot) and not is_choose_place_screen(shot):
            return False
        now = time.time()
        if (
            getattr(self, "_pack_last_list_world", None) == world_id
            and now - float(getattr(self, "_pack_last_list_tap_at", 0.0) or 0.0) < 2.6
        ):
            CONTROL.sleep(0.15)
            return False
        parsed = parse_navigation_rows(shot)
        matches = [
            row
            for row in parsed
            if row.get("world_id") == world_id and row.get("sextant")
        ]
        if world_id == "great_empire":
            nx, ny = min(points, key=lambda item: item[1])
            logger.warning("PACK list GE canonical top row ({:.3f},{:.3f})", nx, ny)
        elif matches:
            chosen = min(matches, key=lambda row: float(row["sextant"][1]))
            nx, ny = float(chosen["sextant"][0]), float(chosen["sextant"][1])
            logger.warning(
                "PACK list OCR {} y={:.3f} blob={}",
                world_id,
                ny,
                compact_ui(str(chosen.get("blob") or ""))[:72],
            )
        else:
            logger.warning(
                "PACK list no OCR {} — fallback не жму ids={}",
                world_id,
                [row.get("world_id") for row in parsed],
            )
            return False
        self._pack_last_list_tap_at = now
        self._pack_last_list_world = world_id
        self._tap_norm_exact(nx, min(0.86, ny + 0.015))
        deadline = time.time() + (1.15 if getattr(self, "_speed_burst_active", lambda: False)() else 2.2)
        while time.time() < deadline:
            CONTROL.sleep(0.12)
            try:
                now = self._image()
            except Exception:
                break
            if is_loading_screen(now):
                continue
            if is_map_screen(now) and len(find_world_list_sextants(now)) < 3:
                grass = map_grass_ratio(now)
                ge_ok = world_id == "great_empire" and grass >= 0.12
                other_ok = world_id != "great_empire" and grass < 0.12
                if not ge_ok and not other_ok:
                    continue
                logger_fn = getattr(self, "_log_pack_hud", None)
                if callable(logger_fn):
                    logger_fn(now, world_id)
                return True
        return True

    def _fast_world_home(self, world_id: str = "great_empire") -> bool:
        """Nav → Выбери место → topmost row left sextant → grass only if the list is short."""
        world_id = str(world_id or "great_empire")
        nx, ny = self._world_list_expected_sextant(world_id)
        logger.warning(
            "SPEED home {}: Навигация → Выбери место → секстант ({:.3f},{:.3f})",
            world_id,
            nx,
            ny,
        )
        try:
            already = self._image()
        except Exception:
            already = None
        if already is not None and getattr(self, "_pack_dismiss_overlay", None):
            self._pack_dismiss_overlay(already)
            try:
                already = self._image()
            except Exception:
                already = None
        if already is not None and (is_travel_dialog(already) or is_formation_screen(already)):
            logger.warning("SPEED home {}: план/поход открыт — Навигацию не трогаю", world_id)
            return False
        matcher = getattr(self, "_pack_hud_matches_world", None)
        if already is not None and callable(matcher) and matcher(already, world_id):
            self._switched_world_id = world_id
            self._need_ge_home = False
            logger.warning("SPEED home {}: уже на нужной карте — Навигацию не открываю", world_id)
            return True
        list_open = already is not None and (
            is_world_list_open(already) or is_choose_place_screen(already)
        )
        if list_open:
            self._tap_open_world_list_row(world_id, already)
        elif time.time() - float(getattr(self, "_pack_last_list_tap_at", 0.0) or 0.0) < 5.0:
            deadline = time.time() + 2.0
            while time.time() < deadline:
                CONTROL.sleep(0.15)
                try:
                    shot = self._image()
                except Exception:
                    break
                if is_map_screen(shot) and not is_world_list_open(shot) and not is_choose_place_screen(shot):
                    break
        else:
            self._tap_norm_exact(0.198, 0.937)
            CONTROL.sleep(0.12)
            self._tap_norm_exact(*SELECT_PLACE_FALLBACK)
            CONTROL.sleep(0.14)
            try:
                opened = self._image()
            except Exception:
                opened = None
            if opened is not None and (
                is_world_list_open(opened) or is_choose_place_screen(opened)
            ):
                self._tap_open_world_list_row(world_id, opened)
            else:
                logger.warning("SPEED home {}: список мест не открылся — карту не тыкаю вслепую", world_id)
        hud_ok = False
        try:
            landed = self._image()
        except Exception:
            landed = None
        matcher = getattr(self, "_pack_hud_matches_world", None)
        if landed is not None and callable(matcher):
            hud_ok = bool(matcher(landed, world_id))
        elif landed is not None and is_map_screen(landed) and not is_world_list_open(landed):
            grass = map_grass_ratio(landed)
            hud_ok = (grass >= 0.12) if world_id == "great_empire" else (grass < 0.12)
        if hud_ok:
            self._switched_world_id = world_id
            self._need_ge_home = False
        else:
            logger.warning("SPEED home {}: HUD/карта не подтвердили мир — охоту не начинаю", world_id)
            self._switched_world_id = None
        self._hunt_queue = []
        if hud_ok and getattr(self, "store", None) is not None and not getattr(
            self, "_speed_burst_active", lambda: False
        )():
            spec = WORLD_BY_ID.get(world_id)
            self.store.live.current_world = spec.display_ru if spec is not None else world_id
            self.store.live.post_attack_home_pending = False
            self.store.save()
        return hud_ok

    def _fast_ge_home(self) -> bool:
        return self._fast_world_home("great_empire")

    def _navigation_home_for_world(self, world_id: str) -> bool:
        if getattr(self, "_speed_burst_active", lambda: False)():
            return self._fast_world_home(world_id)
        opened = self._open_navigation_list()
        if opened is None:
            return False
        scan = self._scan_navigation_list(opened)
        row = self._central_row_for_world(scan, world_id)
        if row is None:
            self._close_navigation_list()
            logger.warning("В навигации не найден точный центральный ряд {}", world_id)
            return False
        logger.info(
            "Навигация fallback geometry: первый центральный ряд {} / row={} sextant={}",
            world_id,
            row.world_ru,
            row.sextant,
        )
        clicked = self._click_world_sextant(row)
        if not clicked:
            return False
        map_image = self._await_world_map(timeout=6)
        if map_image is None or not is_map_screen(map_image):
            logger.warning("Навигация {} не вернула структурно подтверждённую карту", world_id)
            return False
        logger.info(
            "Навигация {} вернула карту; имя/координаты только diagnostic={}",
            world_id,
            self._read_castle_identity(map_image, banner=False)
            if hasattr(self, "_read_castle_identity")
            else {},
        )
        return True

    def _world_hud_coords_match(self, expected: dict[str, Any], world_id: str = "") -> bool:
        """Structural world-HUD check; coordinates are logged but never gate."""
        reader = getattr(self, "_read_castle_identity", None)
        try:
            image = self._image()
        except Exception:
            return False
        hud = find_castle_name_hud_point(image)
        matched = bool(
            is_map_screen(image)
            and hud
            and 0.28 <= hud[0] <= 0.72
            and 0.005 <= hud[1] <= 0.09
            and not is_ruby_plus_hud_point(*hud)
        )
        if matched and world_id == "great_empire" and map_grass_ratio(image) < 0.12:
            matched = False
        observed = reader(image, banner=False) if callable(reader) else {}
        logger.info(
            "Проверка мира по HUD geometry: point={} match={} coords diagnostic expected={} observed={}",
            hud,
            matched,
            expected.get("coords"),
            observed.get("coords"),
        )
        return matched

    def _learn_central_from_hud(self, world_id: str) -> None:
        configured = dict(((self.config.get("central_castles") or {}).get(world_id)) or {})
        if configured.get("name") and configured.get("coords"):
            return
        reader = getattr(self, "_read_castle_identity", None)
        if not callable(reader):
            return
        try:
            observed = reader(self._image(), banner=False)
        except Exception:
            return
        coords = observed.get("coords")
        name = str(observed.get("name") or "").strip()
        if not name or not isinstance(coords, (list, tuple)) or len(coords) != 2:
            logger.warning("HUD центра {} после навигации не читается: {}", world_id, observed)
            return
        registry = dict(getattr(self.store.live, "central_castles", None) or {})
        previous = dict(registry.get(world_id) or {})
        registry[world_id] = {
            **previous,
            "world_id": world_id,
            "world": WORLD_BY_ID[world_id].display_ru,
            "name": name,
            "coords": [int(coords[0]), int(coords[1])],
        }
        self.store.live.central_castles = registry
        self.store.save()
        logger.info(
            "HUD-реестр центра {} подтверждён: {} X:{} Y:{}",
            world_id,
            name,
            coords[0],
            coords[1],
        )

    def _record_world_switch_failure(self, spec: Any, reason: str) -> str:
        self._switched_world_id = None
        self._need_ge_home = True
        tries = dict(getattr(self, "_world_switch_failures", None) or {})
        tries[spec.id] = int(tries.get(spec.id) or 0) + 1
        self._world_switch_failures = tries
        if spec.tour and spec.id != "great_empire" and tries[spec.id] >= 2:
            logger.warning(
                "{}: переход не подтверждён {} раза ({}) — пропускаю мир и иду дальше",
                spec.display_ru,
                tries[spec.id],
                reason,
            )
            if spec.mode_id:
                self.store.skip_mode(spec.mode_id)
            self._publish_world_report(
                getattr(self, "_world_scan", None),
                [f"{spec.display_ru}: переход не подтверждён ({reason}), мир пропущен"],
            )
            return "world_skip_empty"
        return "world_switch_failed"

    def _publish_world_report(self, scan: NavigationScan | None, extra: list[str] | None = None) -> str:
        payload = write_world_attack_report(scan, extra_lines=extra)
        text = str(payload.get("text") or "")
        self.store.live.unopened_worlds = list(payload.get("missing_worlds") or [])
        self.store.live.attack_report = text
        if text:
            self.store.live.last_action = text.split("\n")[0][:80]
        self.store.save()
        logger.info("Отчёт по мирам: {}", text.replace("\n", " | "))
        if getattr(self, "telegram", None) is not None:
            self.telegram.report_status(text)
        return text

    def _open_navigation_list(self) -> Any | None:
        image = self._image()
        if self._dismiss_quit_game_if_open(image):
            image = self._image()
        if getattr(self, "_dismiss_inbox_if_open", None) and self._dismiss_inbox_if_open(image):
            image = self._image()
        if is_world_list_open(image):
            logger.info("Список навигации уже открыт")
            try:
                save_shot(image, "nav-list-already-open.png")
            except Exception:
                pass
            return image
        if is_navigation_submenu(image):
            already_submenu = True
        else:
            already_submenu = False
        if (
            not already_submenu
            and not is_map_screen(image)
            and not is_travel_dialog(image)
        ):
            close = find_parchment_title_close(image)
            if close is None or is_ruby_plus_hud_point(*close) or close[0] < 0.72:
                close = (0.86, 0.055)
            if not is_ruby_plus_hud_point(*close):
                logger.info("Карта закрыта пергаментом — жму крестик ({:.3f}, {:.3f})", *close)
                self._tap_forced(*close)
                CONTROL.sleep(0.45)
                image = self._image()
        if not already_submenu:
            if not is_map_screen(image):
                logger.warning(
                    "Панель Навигации не ищу вне чистой карты (formation={} travel={} inbox={})",
                    is_formation_screen(image),
                    is_travel_dialog(image),
                    is_inbox_screen(image),
                )
                return None
            point = find_navigation_button(image)
            if point is None:
                footer_text = compact_ui(ocr_text_ui(crop_rel(image, [0.0, 0.90, 0.40, 1.0]), psm=6))
                nav_markers = ("навигац", "hasura", "habura", "navigation")
                for page in range(4):
                    if any(marker in footer_text for marker in nav_markers):
                        break
                    logger.info(
                        "Навигация скрыта на странице панели {} — жму левую стрелку панели",
                        page + 1,
                    )
                    self._tap_norm_exact(0.055, 0.94)
                    CONTROL.sleep(0.55)
                    image = self._image()
                    point = find_navigation_button(image)
                    if point is not None:
                        break
                    footer_text = compact_ui(
                        ocr_text_ui(crop_rel(image, [0.0, 0.90, 0.40, 1.0]), psm=6)
                    )
            if point is None:
                point = find_navigation_button(image)
            if point is None and is_map_screen(image):
                point = (0.198, 0.937)
                logger.info(
                    "Навигация: шаблон/OCR не подтвердили звезду — жму запасную точку ({:.3f}, {:.3f})",
                    *point,
                )
            if point is None:
                logger.warning("Безопасный центр кнопки «Навигация» не найден")
                return None
            if (
                is_ruby_plus_hud_point(*point)
                or point[0] < 0.07
                or point[0] >= 0.26
                or point[1] < 0.87
            ):
                logger.warning("Не жму рубины/край экрана вместо Навигации ({:.3f}, {:.3f})", *point)
                return None
            logger.info("Жму «Навигация» ({:.3f}, {:.3f})", point[0], point[1])
            burst = bool(getattr(self, "_speed_burst_active", lambda: False)())
            if not burst:
                try:
                    save_shot(image, "nav-before-click.png")
                except Exception:
                    pass
            self._tap_norm_exact(*point)
            CONTROL.sleep(0.12 if burst else 0.85)
        submenu = self._wait_for(
            lambda img: is_navigation_submenu(img) or is_world_list_open(img),
            timeout=5,
            label="меню навигации",
        )
        if submenu is None:
            submenu = self._image()
        if is_world_list_open(submenu):
            logger.info("Список навигации открыт сразу")
            try:
                save_shot(submenu, "nav-list-open.png")
            except Exception:
                pass
            return submenu
        if not is_navigation_submenu(submenu):
            logger.warning("После Навигации нет трёх кнопок — «Выбери место» не жму")
            try:
                save_shot(submenu, "nav-submenu-missing.png")
            except Exception:
                pass
            return None
        place = find_select_place_button(submenu)
        if place is None or not is_select_place_point(*place):
            if place is not None:
                logger.info(
                    "Пропускаю клик ({:.3f}, {:.3f}) — это «Карта» или «В замок», беру «Выбери место»",
                    place[0],
                    place[1],
                )
            place = SELECT_PLACE_FALLBACK
        logger.info(
            "После Навигации три кнопки — жму среднюю «Выбери место» ({:.3f}, {:.3f}), не Карта и не В замок",
            place[0],
            place[1],
        )
        self._tap_norm_exact(*place)
        burst = bool(getattr(self, "_speed_burst_active", lambda: False)())
        CONTROL.sleep(0.15 if burst else 1.2)
        opened = self._wait_for(is_world_list_open, timeout=2 if burst else 8, label="список миров")
        if opened is None:
            shot = self._image()
            from e4kbot.vision import _navigation_ocr_blob, _navigation_ocr_confirms_list, parse_navigation_rows
            if not burst and (
                _navigation_ocr_confirms_list(_navigation_ocr_blob(shot))
                or any(item.get("sextant") for item in parse_navigation_rows(shot))
            ):
                logger.info("Список миров распознан без шаблона")
                opened = shot
        if opened is None:
            logger.warning("Список навигации не открылся")
            if not burst:
                try:
                    save_shot(self._image(), "nav-list-not-open.png")
                except Exception:
                    pass
        else:
            logger.info("Список навигации открыт")
            if not burst:
                try:
                    save_shot(opened, "nav-list-open.png")
                except Exception:
                    pass
        return opened

    def _close_navigation_list(self) -> None:
        image = self._image()
        if not is_world_list_open(image):
            return
        # Full-screen «Выбери место» closes with the title-bar back arrow, not map grass.
        close = find_parchment_title_close(image)
        if close is None or is_ruby_plus_hud_point(*close) or close[0] < 0.04:
            close = (0.165, 0.042)
        logger.info("Закрываю список навигации стрелкой ({:.3f}, {:.3f})", close[0], close[1])
        self._tap_forced(*close)
        CONTROL.sleep(0.55)
        still = self._image()
        if is_world_list_open(still) or is_choose_place_screen(still):
            logger.info("Список ещё открыт — ещё раз стрелка назад")
            self._tap_forced(0.165, 0.042)
            CONTROL.sleep(0.55)
        self._dismiss_quit_game_if_open()


    def _scroll_place_list(self, image: Any | None = None) -> None:
        """Reveal lower owned-place rows. Drag the row text, never the left sextant."""
        shot = image if image is not None else self._image()
        points = find_world_list_sextants(shot)
        if points:
            _nx, ny = points[-1]
            start = (0.50, min(0.58, max(0.20, ny)))
            finish = (0.50, max(0.14, start[1] - 0.20))
        else:
            start, finish = (0.50, 0.32), (0.50, 0.14)
        size = self._size()
        width, height = size
        logger.info(
            "Листаю список «Выбери место» ({:.3f},{:.3f})→({:.3f},{:.3f})",
            start[0],
            start[1],
            finish[0],
            finish[1],
        )
        if getattr(self, "adb", None) is not None:
            self.adb.wheel(
                round(start[0] * width),
                round(start[1] * height),
                delta=-720,
                source_size=size,
            )
            CONTROL.sleep(0.12)
            self.adb.swipe(
                round(start[0] * width),
                round(start[1] * height),
                round(finish[0] * width),
                round(finish[1] * height),
                duration_ms=700,
                source_size=size,
            )
        else:
            self._swipe_norm(start, finish)
        CONTROL.sleep(0.45)

    def _scan_navigation_list(self, image: Any | None = None) -> NavigationScan:
        shot = image if image is not None else self._image()
        rows: list[NavRow] = []
        blobs: list[str] = []
        seen: set[tuple[str, str]] = set()
        for page in range(5):
            parsed = parse_navigation_rows(shot)
            from e4kbot.vision import ocr_text_ui, crop_rel
            blobs.append(ocr_text_ui(crop_rel(shot, [0.06, 0.10, 0.94, 0.78]), psm=6))
            for item in parsed:
                blob = str(item.get("blob") or "")
                blobs.append(blob)
                sextant = item.get("sextant")
                if not sextant:
                    continue
                nx, ny = float(sextant[0]), float(sextant[1])
                if nx >= 0.40:
                    logger.warning(
                        "Правую кнопку замка не жму ({:.3f}, {:.3f}) — беру только левый секстант",
                        nx,
                        ny,
                    )
                    continue
                world_id = item.get("world_id")
                spec = WORLD_BY_ID.get(str(world_id)) if world_id else None
                key = (str(world_id or ""), f"{ny:.2f}")
                if key in seen:
                    continue
                seen.add(key)
                rows.append(
                    NavRow(
                        world_id=str(world_id) if world_id else None,
                        world_ru=spec.display_ru if spec else "",
                        castle=blob.split("\n")[0][:40] if blob else "",
                        coords=item.get("coords"),
                        sextant=(nx, ny),
                        blob=blob,
                    )
                )
            scan = scan_from_blob("\n".join(blobs), rows)
            logger.info(
                "Страница мест {}: {}",
                page + 1,
                [item.world_id or "?" for item in rows],
            )
            try:
                save_shot(shot, f"nav-list-page-{page + 1}.png")
            except Exception:
                pass
            self._world_list_shot = shot
            if is_place_list_exhausted(shot):
                logger.info("Список мест полный — ниже пустой пергамент, не листаю")
                break
            if len(scan.open_world_ids) >= 3:
                break
            if page >= 4:
                break
            self._scroll_place_list(shot)
            shot = self._image()
            if not is_world_list_open(shot) and not is_choose_place_screen(shot):
                logger.info("Список мест закрылся при прокрутке")
                break
        scan = scan_from_blob("\n".join(blobs), rows)
        logger.info(
            "Навигация: открыты {}, нет {}",
            [WORLD_BY_ID[item].display_ru for item in scan.open_world_ids],
            [WORLD_BY_ID[item].display_ru for item in scan.missing_world_ids],
        )
        if "storm_islands" in scan.missing_world_ids:
            logger.info(STORM_UNOPENED_LINE)
        self._register_central_castles(scan)
        return scan

    def _click_world_sextant(self, row: NavRow) -> bool:
        nx, ny = row.sextant
        if nx >= 0.40 or is_ruby_plus_hud_point(nx, ny):
            logger.warning("Секстант мира слева невалиден ({:.3f}, {:.3f})", nx, ny)
            return False
        # Green template matching is biased toward the icon's upper ornament.
        # Tap the visual center of the same button, not the row above it.
        ny = min(0.86, ny + 0.015)
        logger.info(
            "Левый секстант мира {} ({:.3f}, {:.3f})",
            row.world_ru or row.world_id or "?",
            nx,
            ny,
        )
        self._tap_norm_exact(nx, ny)
        burst = bool(getattr(self, "_speed_burst_active", lambda: False)())
        CONTROL.sleep(0.12 if burst else 1.2)
        self._dismiss_quit_game_if_open()
        if not burst and getattr(self, "wait_out_loading", None):
            self.wait_out_loading(timeout=40)
        image = self._image() if burst else (self._await_world_map(timeout=18) or self._image())
        if is_world_list_open(image) or is_choose_place_screen(image):
            return False
        if is_map_screen(image):
            return True
        logger.info("После секстанта список закрыт — считаю переход в мир успешным")
        return not is_formation_screen(image)

    def _ensure_world(self, kind: str) -> str:
        spec = spec_for_kind(kind)
        if spec is None:
            return "ok"
        try:
            if self._dismiss_blocking_overlay():
                CONTROL.sleep(0.05 if getattr(self, "_speed_burst_active", lambda: False)() else 0.5)
        except Exception:
            pass
        if getattr(self, "_speed_burst_active", lambda: False)():
            current = getattr(self, "_switched_world_id", None)
            need_ge_home = bool(getattr(self, "_need_ge_home", False)) and spec.id == "great_empire"
            if current == spec.id and not need_ge_home:
                return "ok"
            self._fast_world_home(spec.id)
            return "ok"
        current = getattr(self, "_switched_world_id", None)
        need_ge_home = bool(getattr(self, "_need_ge_home", False)) and spec.id == "great_empire"
        if current == spec.id and not need_ge_home:
            expected_current = self._configured_central(spec.id)
            if not spec.tour or self._world_hud_coords_match(expected_current, spec.id):
                return "ok"
            logger.warning("{} записан текущим, но HUD geometry не совпала — fallback Навигация", spec.display_ru)
            self._switched_world_id = None
            if spec.id == "great_empire":
                self._need_ge_home = True
                need_ge_home = True
        if not spec.tour and not current and not need_ge_home:
            return "ok"
        if spec.id == "great_empire" and need_ge_home:
            expected_home = self._configured_central(spec.id)
            try:
                home_shot = self._image()
            except Exception:
                home_shot = None
            grassy = bool(home_shot is not None and map_grass_ratio(home_shot) >= 0.12)
            if grassy and self._world_hud_coords_match(expected_home, spec.id):
                logger.info(
                    "Уже на карте Великой империи у центра — охоту начинаю, Навигацию на старте не открываю"
                )
                self._switched_world_id = spec.id
                self._need_ge_home = False
                self.store.live.current_world = spec.display_ru
                self.store.save()
                if hasattr(self, "_finish_verified_home"):
                    self._finish_verified_home()
                return "ok"
        opened = self._open_navigation_list()
        if opened is None:
            if spec.tour and spec.id != "great_empire":
                return self._record_world_switch_failure(spec, "список навигации не открылся")
            return "nav_not_found"
        scan = self._scan_navigation_list(opened)
        self._world_scan = scan
        self._publish_world_report(scan)
        baron = dict(self.config.get("baron_attacks") or {})
        baron["kingdom"] = int(spec.kingdom_id)
        self.config["baron_attacks"] = baron
        missing_from_tour = spec.tour and spec.id not in scan.open_world_ids
        row = self._central_row_for_world(scan, spec.id)
        only_great_empire = (
            any(item.world_id == "great_empire" for item in scan.rows)
            and not scan.open_world_ids
        )
        list_shot = getattr(self, "_world_list_shot", None) or opened
        list_done = is_place_list_exhausted(list_shot)
        if spec.tour and only_great_empire and not list_done:
            logger.warning(
                "В «Выбери место» видна только Великая империя — список не прокрутился, повторю"
            )
            self._close_navigation_list()
            return "nav_not_found"
        if spec.tour and only_great_empire and list_done:
            logger.info(
                "В «Выбери место» только Великая империя и пустой пергамент — не жму Карта и не В замок"
            )
        if missing_from_tour:
            extra = []
            if spec.id == "storm_islands":
                extra.append(STORM_UNOPENED_LINE)
            extra.append(CONTINUE_ONE_WORLD_LINE)
            self._close_navigation_list()
            self._publish_world_report(scan, extra)
            logger.warning(
                "{} — мира нет в списке, пропускаю и продолжаю строгий тур",
                spec.display_ru,
            )
            if spec.mode_id:
                self.store.skip_mode(spec.mode_id)
            return "world_skip_empty"
        if spec.tour and row is None:
            logger.warning(
                "Мир {} виден, но точный центральный ряд не подтверждён",
                spec.display_ru,
            )
            self._close_navigation_list()
            return self._record_world_switch_failure(spec, "точный центральный ряд не найден")
        if row is None:
            self._close_navigation_list()
            if need_ge_home:
                logger.warning("Старт с Великой империи обязателен — ряд не найден, повторю, не охочусь в другом мире")
                return "world_switch_failed"
            self._need_ge_home = False
            logger.info("Великая империя — ряд не обязателен, продолжаю охоту")
            return "ok"
        if not self._click_world_sextant(row):
            logger.warning("Не удалось перейти в мир {}", spec.display_ru)
            return self._record_world_switch_failure(spec, "левый секстант не открыл мир")
        expected_central = self._configured_central(spec.id)
        if not self._world_hud_coords_match(expected_central, spec.id):
            logger.warning(
                "Переход в {} отклонён: top-center HUD geometry не подтверждена; coords diagnostic={}",
                spec.display_ru,
                expected_central.get("coords"),
            )
            return self._record_world_switch_failure(spec, "top-center HUD geometry не подтверждена")
        if not expected_central.get("coords"):
            self._learn_central_from_hud(spec.id)
        self._switched_world_id = spec.id
        failures = dict(getattr(self, "_world_switch_failures", None) or {})
        failures.pop(spec.id, None)
        self._world_switch_failures = failures
        if spec.id == "great_empire":
            self._need_ge_home = False
        self._hunt_queue = []
        self._blocked_screen_targets = []
        self.store.live.current_world = spec.display_ru
        self.store.save()
        logger.info("Мир {} — центральный замок, ищу цели", spec.display_ru)
        if hasattr(self, "_finish_verified_home"):
            self._finish_verified_home()
        return "ok"

    def _recenter_current_world_via_nav(self, kind: str) -> bool:
        spec = spec_for_kind(kind)
        if spec is None:
            return False
        logger.info(
            "Целей нет — Навигация, левый секстант текущего мира {}",
            spec.display_ru,
        )
        opened = self._open_navigation_list()
        if opened is None:
            return False
        scan = self._scan_navigation_list(opened)
        row = self._central_row_for_world(scan, spec.id)
        if row is None:
            self._close_navigation_list()
            return False
        ok = self._click_world_sextant(row)
        self._hunt_queue = []
        self._blocked_screen_targets = []
        if ok and hasattr(self, "_finish_verified_home"):
            self._finish_verified_home()
        return ok

    def _plaque_title_blob(self, image: Any) -> str:
        parts = [
            ocr_text_ui(crop_rel(image, [0.16, 0.10, 0.84, 0.34]), psm=6),
            ocr_text_ui(crop_rel(image, [0.16, 0.18, 0.84, 0.48]), psm=6),
        ]
        return " ".join(parts)

    def _world_target_title_ok(self, image: Any, kind: str) -> bool:
        if not is_world_npc_kind(kind):
            return True
        if is_special_offers_screen(image):
            logger.info("Спецпредложения, не башня — не жму Нападение")
            return False
        blob = self._plaque_title_blob(image)
        ok = match_target_kind(blob, kind)
        logger.info("OCR цели мира {}: {} / {}", kind, ok, blob[:90].replace("\n", " "))
        if looks_like_wrong_world_target(blob) and not ok:
            logger.info("Табличка замка/Обзор — не башня этого мира, закрываю")
            return False
        if ok:
            return True
        hay = compact_ui(blob) if blob else ""
        if any(token in hay for token in ("предлож", "спец", "магазин", "рубин", "offer", "shop")):
            return False
        npc = any(
            token in hay
            for token in ("башн", "tower", "форт", "fort", "варвар", "пустын", "культ", "ураган")
        )
        if npc:
            return True
        if is_info_plaque(image) or find_plaque_attack_button(image) or find_world_parchment_attack_button(image):
            logger.info("Пергамент/Нападение без OCR имени — жму Нападение")
            return True
        return False

    def _dismiss_wrong_world_target(self, image: Any) -> None:
        close = find_parchment_title_close(image)
        if close and not is_ruby_plus_hud_point(*close):
            self._tap_norm_exact(*close)
            CONTROL.sleep(0.4)
            return
        if not is_formation_screen(image) and not is_travel_dialog(image):
            self._tap_norm_exact(0.50, 0.18)
            CONTROL.sleep(0.35)

    def _cancel_world_attack(self) -> None:
        """Cancel an unfilled world attack. Never Esc."""
        image = self._image()
        if self._picker_overlay_open(image):
            cancel = (self.layout.get("buttons") or {}).get("picker_cancel")
            if cancel:
                self._tap_norm_exact(float(cancel[0]), float(cancel[1]))
                CONTROL.sleep(0.45)
                image = self._image()
        if is_travel_dialog(image):
            self.tap_rel("travel_cancel")
            CONTROL.sleep(0.45)
            image = self._image()
        if is_formation_screen(image):
            self.close_formation_plan()
        CONTROL.sleep(0.4)
        self._dismiss_quit_game_if_open()

    def _open_second_wave(self) -> bool:
        image = self._image()
        point = find_add_wave_control(image)
        if point is None:
            logger.warning("Вторую волну не нашёл")
            return False
        if is_ruby_plus_hud_point(*point):
            return False
        logger.info("Открываю вторую волну ({:.3f}, {:.3f})", point[0], point[1])
        self._tap_norm_exact(*point)
        CONTROL.sleep(0.12 if getattr(self, "_speed_burst_active", lambda: False)() else 0.55)
        center = (self.layout.get("buttons") or {}).get("center_flank") or [0.138, 0.51]
        self._tap_norm_exact(float(center[0]), float(center[1]))
        CONTROL.sleep(0.08 if getattr(self, "_speed_burst_active", lambda: False)() else 0.35)
        return True

    def _prepare_waves_for_kind(self, kind: str) -> tuple[bool, str]:
        self._wave_kind = kind
        spec = spec_for_kind(kind)
        waves = int(spec.waves) if spec is not None else 1
        center = (self.layout.get("buttons") or {}).get("center_flank")
        if center and (spec is None or spec.center_only):
            self._tap_norm_exact(float(center[0]), float(center[1]))
            CONTROL.sleep(0.08 if getattr(self, "_speed_burst_active", lambda: False)() else 0.25)
        ok, reason = self._prepare_single_center_wave()
        if not ok:
            return False, reason
        if waves < 2:
            return True, ""
        if not self._open_second_wave():
            return False, "second_wave_not_found"
        ok, reason = self._prepare_single_center_wave()
        if not ok:
            return False, reason or "second_wave_not_full"
        return True, ""

    def _handle_world_fill_failure(self, kind: str, reason: str) -> str:
        spec = spec_for_kind(kind)
        if spec is None:
            self._cancel_world_attack()
            return "unsafe_formation"
        if getattr(self, "_speed_burst_active", lambda: False)():
            occupancy = None
            try:
                shot = self._image()
                occupancy = self._read_ratio_from_image(shot, "picker_units") or self._read_ratio_from_image(
                    shot, "formation_units"
                )
            except Exception:
                occupancy = None
            last = getattr(self, "_last_picker_fill", None)
            empty = bool(
                (occupancy is not None and occupancy[0] <= 0 and occupancy[1] > 0)
                or (last is not None and last[0] <= 0 and last[1] > 0)
            )
            if reason == "soldiers_depleted" and empty:
                logger.warning(
                    "PACK: армия пустая occupancy={} last={} — мир {}",
                    occupancy,
                    last,
                    spec.display_ru,
                )
            else:
                logger.warning(
                    "PACK: набор OCR {} occupancy={} last={} — повторяю, мир {} не пропускаю",
                    reason,
                    occupancy,
                    last,
                    spec.display_ru,
                )
                return "unsafe_formation"
        mode_id = spec.mode_id
        sent = int((self.store.live.session_by_mode or {}).get(mode_id) or 0)
        quota = spec.attacks
        for item in ((self.config.get("campaign") or {}).get("queue") or []):
            if str(item.get("mode")) == mode_id:
                quota = max(1, int(item.get("count") or spec.attacks))
                break
        in_flight = len(self.store.in_flight())
        decision = world_fill_decision(
            sent,
            quota,
            in_flight,
            first_must_skip=spec.skip_if_first_cannot_fill,
            wait_last=spec.wait_last_if_cannot_fill,
        )
        logger.warning(
            "Мир {}: набор не 100% ({}) sent={} quota={} in_flight={} → {}",
            spec.display_ru,
            reason,
            sent,
            quota,
            in_flight,
            decision,
        )
        self._cancel_world_attack()
        self.store.skip_mode(mode_id)
        extra = [
            f"{spec.display_ru}: не хватает войск для 100% волн — мир пропущен",
            CONTINUE_ONE_WORLD_LINE,
        ]
        self._publish_world_report(getattr(self, "_world_scan", None), extra)
        return "world_skip_empty"

    def _handle_world_no_targets(self, kind: str) -> str:
        """Empty hunt stays in this world until 5 successful sends or troops cannot fill."""
        spec = spec_for_kind(kind)
        if spec is None:
            return "no_targets"
        sent = int((self.store.live.session_by_mode or {}).get(spec.mode_id) or 0)
        need = max(0, int(spec.attacks) - sent)
        logger.info(
            "Мир {}: целей нет в этом кадре — остаюсь (успешных {}, нужно ещё {}), не переключаю",
            spec.display_ru,
            sent,
            need,
        )
        self._world_recenter_tries = 0
        return "no_targets"
