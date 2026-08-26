from __future__ import annotations

import time
from typing import Any

from loguru import logger

from e4kbot.bluestacks import save_shot
from e4kbot.control import CONTROL
from e4kbot.vision import (
    find_add_wave_control,
    find_navigation_button,
    find_parchment_title_close,
    find_world_parchment_attack_button,
    find_select_place_button,
    find_world_list_sextants,
    is_choose_place_screen,
    is_formation_screen,
    is_info_plaque,
    is_map_screen,
    is_navigation_submenu,
    is_place_list_exhausted,
    is_ruby_plus_hud_point,
    is_select_place_point,
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


class WorldSwitchMixin:
    """Navigation parchment world travel + 100% wave policy for other kingdoms."""

    def _kind_spec(self, kind: str):
        return spec_for_kind(kind)

    def _kind_min_fill(self, kind: str) -> float:
        spec = spec_for_kind(kind)
        if spec is None:
            return float((self.config.get("vision") or {}).get("minimum_flank_fill") or 0.70)
        return float(spec.fill_ratio)

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
        if is_world_list_open(image):
            logger.info("Список навигации уже открыт")
            try:
                save_shot(image, "nav-list-already-open.png")
            except Exception:
                pass
            return image
        already_submenu = is_navigation_submenu(image)
        if not already_submenu:
            point = find_navigation_button(image)
            if point is None:
                point = (0.20, 0.937)
            if (
                is_ruby_plus_hud_point(*point)
                or point[0] < 0.08
                or point[0] >= 0.24
                or point[1] < 0.88
            ):
                logger.warning("Не жму рубины/край экрана вместо Навигации ({:.3f}, {:.3f})", *point)
                point = (0.20, 0.937)
            logger.info("Жму «Навигация» ({:.3f}, {:.3f})", point[0], point[1])
            try:
                save_shot(image, "nav-before-click.png")
            except Exception:
                pass
            self._tap_norm_exact(*point)
            CONTROL.sleep(0.85)
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
        CONTROL.sleep(1.2)
        opened = self._wait_for(is_world_list_open, timeout=8, label="список миров")
        if opened is None:
            shot = self._image()
            from e4kbot.vision import _navigation_ocr_blob, _navigation_ocr_confirms_list, parse_navigation_rows
            if _navigation_ocr_confirms_list(_navigation_ocr_blob(shot)) or any(
                item.get("sextant") for item in parse_navigation_rows(shot)
            ):
                logger.info("Список миров распознан без шаблона")
                opened = shot
        if opened is None:
            logger.warning("Список навигации не открылся")
            try:
                save_shot(self._image(), "nav-list-not-open.png")
            except Exception:
                pass
        else:
            logger.info("Список навигации открыт")
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
        return scan

    def _click_world_sextant(self, row: NavRow) -> bool:
        nx, ny = row.sextant
        if nx >= 0.40 or is_ruby_plus_hud_point(nx, ny):
            logger.warning("Секстант мира слева невалиден ({:.3f}, {:.3f})", nx, ny)
            return False
        logger.info(
            "Левый секстант мира {} ({:.3f}, {:.3f})",
            row.world_ru or row.world_id or "?",
            nx,
            ny,
        )
        self._tap_norm_exact(nx, ny)
        CONTROL.sleep(1.2)
        self._dismiss_quit_game_if_open()
        if getattr(self, "wait_out_loading", None):
            self.wait_out_loading(timeout=40)
        image = self._await_world_map(timeout=18) or self._image()
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
        current = getattr(self, "_switched_world_id", None)
        need_ge_home = bool(getattr(self, "_need_ge_home", False)) and spec.id == "great_empire"
        if current == spec.id and not need_ge_home:
            return "ok"
        if not spec.tour and not current and not need_ge_home:
            return "ok"
        opened = self._open_navigation_list()
        if opened is None:
            return "nav_not_found"
        scan = self._scan_navigation_list(opened)
        self._world_scan = scan
        self._publish_world_report(scan)
        baron = dict(self.config.get("baron_attacks") or {})
        baron["kingdom"] = int(spec.kingdom_id)
        self.config["baron_attacks"] = baron
        missing_from_tour = spec.tour and spec.id not in scan.open_world_ids
        row = scan.row_for_world(spec.id)
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
            tries = getattr(self, "_unopened_tries", None) or {}
            tries[spec.id] = int(tries.get(spec.id) or 0) + 1
            self._unopened_tries = tries
            extra = []
            if spec.id == "storm_islands":
                extra.append(STORM_UNOPENED_LINE)
            extra.append(CONTINUE_ONE_WORLD_LINE)
            extra.append(f"повтор навигации {tries[spec.id]}")
            self._close_navigation_list()
            self._publish_world_report(scan, extra)
            max_tries = 15 if spec.id == "storm_islands" else 6
            if tries[spec.id] < max_tries:
                logger.warning(
                    "{} — мира нет в списке, не пропускаю, повтор навигации {}/{}",
                    spec.display_ru,
                    tries[spec.id],
                    max_tries,
                )
                return "world_unopened"
            logger.warning(
                "{} — мира нет в списке после {} попыток, {}",
                spec.display_ru,
                tries[spec.id],
                CONTINUE_ONE_WORLD_LINE,
            )
            if spec.mode_id and spec.id != "storm_islands":
                self.store.skip_mode(spec.mode_id)
            return "world_unopened"
        if spec.tour and row is None:
            logger.warning(
                "Мир {} в «Выбери место» виден, но левый секстант не взял — повторю, не пропускаю",
                spec.display_ru,
            )
            self._close_navigation_list()
            return "world_switch_failed"
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
            return "world_switch_failed"
        self._switched_world_id = spec.id
        if spec.id == "great_empire":
            self._need_ge_home = False
        self._hunt_queue = []
        self._blocked_screen_targets = []
        self.store.live.current_world = spec.display_ru
        self.store.save()
        logger.info("Мир {} — центральный замок, ищу цели", spec.display_ru)
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
        row = scan.row_for_world(spec.id)
        if row is None:
            self._close_navigation_list()
            return False
        ok = self._click_world_sextant(row)
        self._hunt_queue = []
        self._blocked_screen_targets = []
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
        blob = self._plaque_title_blob(image)
        ok = match_target_kind(blob, kind)
        logger.info("OCR цели мира {}: {} / {}", kind, ok, blob[:90].replace("\n", " "))
        if looks_like_wrong_world_target(blob) and not ok:
            logger.info("Табличка замка/Обзор — не башня этого мира, закрываю")
            return False
        if ok:
            return True
        hay = compact_ui(blob) if blob else ""
        npc = any(
            token in hay
            for token in ("башн", "tower", "форт", "fort", "варвар", "пустын", "культ", "ураган")
        )
        gold = find_world_parchment_attack_button(image)
        if gold is not None or npc:
            logger.info("Пергамент Нападение в текущем мире — OCR имени слабый, не закрываю")
            return True
        if is_info_plaque(image) and not looks_like_wrong_world_target(blob):
            logger.info("Пергамент без OCR имени — не закрываю, жму Нападение по разметке")
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
        CONTROL.sleep(0.55)
        center = (self.layout.get("buttons") or {}).get("center_flank") or [0.138, 0.51]
        self._tap_norm_exact(float(center[0]), float(center[1]))
        CONTROL.sleep(0.35)
        return True

    def _prepare_waves_for_kind(self, kind: str) -> tuple[bool, str]:
        self._wave_kind = kind
        spec = spec_for_kind(kind)
        waves = int(spec.waves) if spec is not None else 1
        center = (self.layout.get("buttons") or {}).get("center_flank")
        if center and (spec is None or spec.center_only):
            self._tap_norm_exact(float(center[0]), float(center[1]))
            CONTROL.sleep(0.25)
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
        if decision == "wait_return":
            if reason == "unit_picker_not_found":
                image = self._image()
                if not (
                    is_formation_screen(image) or self._picker_overlay_open(image)
                ):
                    logger.warning(
                        "Пикер не найден и плана нет — не жду войска, ищу следующую башню"
                    )
                    return "unsafe_formation"
            nearest = self.store.next_return_at()
            wait_until = float(nearest or (time.time() + 90))
            self.store.live.next_attack_at = wait_until
            self.store.live.mode = "wait_commanders"
            self.store.live.last_action = (
                f"{spec.display_ru}: жду возврат военачальника для атаки {sent + 1}/{quota}"
            )
            self.store.save()
            logger.info(
                "Жду возврат {:.0f}с — план не закрываю, не пропускаю атаку {}/{}",
                max(0, wait_until - time.time()),
                sent + 1,
                quota,
            )
            return "wait_return"
        self._cancel_world_attack()
        self.store.skip_mode(mode_id)
        extra = [
            f"{spec.display_ru}: первая атака без 100% волн — мир пропущен",
            CONTINUE_ONE_WORLD_LINE,
        ]
        self._publish_world_report(getattr(self, "_world_scan", None), extra)
        return "world_skip_empty"
