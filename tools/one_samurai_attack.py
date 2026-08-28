"""One live samurai-camp attack at a user-provided coordinate. No second bot."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from loguru import logger

from e4kbot.attacks.samurai_camps import SamuraiCampsModule
from e4kbot.bluestacks import AdbClient, save_shot
from e4kbot.client import (
    BlueStacksEngine,
    HuntTarget,
    SAMURAI_CAMP_COORD_TOLERANCE,
    samurai_coords_allowed,
)
from e4kbot.config import load_config
from e4kbot.control import CONTROL
from e4kbot.paths import DATA_DIR, LOG_DIR, ensure_dirs
from e4kbot.state import StateStore
from e4kbot.telegram_bot import TelegramReporter
from e4kbot.vision import (
    find_samurai_candidates,
    is_formation_screen,
    is_start_attack_gate,
    is_travel_dialog,
    project_map_coordinate,
)

CAMPS = ((603, 736), (605, 734), (606, 733), (606, 731))
TARGET = CAMPS[3]
DEADLINE_SECONDS = 360
MAX_PAN_STEPS = 8


def _closest_tent(
    hits: list[tuple[float, float, float]],
    viewport: tuple[int, int] | None,
    target: tuple[int, int],
    vision: dict,
) -> tuple[float, HuntTarget] | None:
    if not hits:
        return None
    anchor = vision.get("map_anchor") or [0.50, 0.54]
    scale = vision.get("map_coordinate_scale") or [0.044, 0.044]
    best: tuple[float, HuntTarget] | None = None
    for nx, ny, score in hits:
        coords = None
        if viewport is not None:
            projected = project_map_coordinate(
                (nx, ny),
                viewport,
                (float(anchor[0]), float(anchor[1])),
                (float(scale[0]), float(scale[1])),
            )
            coords = (round(projected[0]), round(projected[1]))
        item = HuntTarget((nx, ny), coords)
        dist = (
            99.0
            if coords is None
            else float(abs(coords[0] - target[0]) + abs(coords[1] - target[1]))
        )
        logger.info("  шатёр оценка {} экран ({:.3f},{:.3f}) score={:.3f}", coords, nx, ny, score)
        if best is None or dist < best[0]:
            best = (dist, item)
    return best


def _pick_visible(client: BlueStacksEngine, target: tuple[int, int]) -> HuntTarget | None:
    """Find the listed camp on the map, panning toward its coords if the frame is empty."""
    offsets = client._map_scan_offsets()
    scan_index = 0
    last_image = None
    for step in range(MAX_PAN_STEPS + 1):
        image = client._image()
        last_image = image
        client._dismiss_reward_popups(image)
        client._dismiss_special_offers_if_open(image)
        client._dismiss_hire_menu_if_open()
        client._dismiss_inbox_if_open()
        image = client._image()
        last_image = image
        hits = find_samurai_candidates(image, max_hits=16)
        _main, viewport = client._read_map_coords(image)
        vision = client.config.get("vision") or {}
        logger.info(
            "Шатров на кадре: {} вьюпорт {} шаг {}/{}",
            len(hits),
            viewport,
            step,
            MAX_PAN_STEPS,
        )
        closest = _closest_tent(hits, viewport, target, vision)
        if closest is not None and closest[0] <= SAMURAI_CAMP_COORD_TOLERANCE:
            chosen = HuntTarget(closest[1].point, target)
            logger.info(
                "Бью {} через шатёр на ({:.3f}, {:.3f}), оценка {}, Δ={}",
                target,
                chosen.point[0],
                chosen.point[1],
                closest[1].coords,
                closest[0],
            )
            return chosen
        if closest is not None:
            logger.info(
                "Ближайший шатёр {} Δ={} к {} — двигаю карту, не бью чужое",
                closest[1].coords,
                closest[0],
                target,
            )
        elif not hits:
            logger.info("На экране нет шатров — двигаю карту к {}", target)
        if step >= MAX_PAN_STEPS:
            break
        moved = client._pan_toward_coords(target)
        if moved:
            continue
        if not offsets:
            break
        dx, dy = offsets[scan_index % len(offsets)]
        scan_index += 1
        logger.info("Локальный скан карты ({:+.2f}, {:+.2f})", dx, dy)
        client._pan_map(dx, dy)
        CONTROL.sleep(0.6)
    if last_image is not None:
        save_shot(last_image, "samurai_test_no_camps.png")
    logger.warning("Лагерь {} на карте не нашёл после сдвигов — не бью вслепую", target)
    return None


def main() -> int:
    ensure_dirs()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    payload = {"camps": [list(item) for item in CAMPS], "test_target": list(TARGET)}
    (DATA_DIR / "samurai_targets.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    logger.remove()
    logger.add(sys.stderr, level="INFO")
    logger.add(
        LOG_DIR / "bot_{time:YYYY-MM-DD}.log",
        rotation="1 day",
        encoding="utf-8",
        level="DEBUG",
    )
    config = load_config()
    config["dry_run"] = False
    config["samurai_preset_ready"] = False
    CONTROL.configure(config, startup=True)
    CONTROL.enable()
    store = StateStore()
    store.live.dry_run = False
    store.live.mode = "attack"
    store.live.active_mode = "samurai_camps"
    store.save()
    telegram = TelegramReporter(config)
    adb = AdbClient(config)
    try:
        adb.connect()
    except Exception:
        logger.warning("ADB нет — клики мышью")
    client = BlueStacksEngine(config, store, telegram, adb)
    client._qualifying_cycles = 10
    client._speed_burst_done = True
    client._selected_target_coords = TARGET
    if not samurai_coords_allowed(TARGET):
        logger.error("Цель {} не в списке шатров data/samurai_targets.json — не бью", TARGET)
        return 6
    image = client._image()
    if is_formation_screen(image) or is_start_attack_gate(image) or is_travel_dialog(image):
        logger.error(
            "План/поход уже открыт — закрываю, не продолжаю непроверенную цель (возможен замок игрока)"
        )
        if is_travel_dialog(image):
            from e4kbot.vision import find_travel_seal_pair

            pair = find_travel_seal_pair(image)
            if pair is not None:
                _green, red = pair
                client._tap_norm_exact(*red)
            else:
                client._tap_norm_exact(0.23, 0.815)
            CONTROL.sleep(0.4)
        else:
            client.close_formation_plan()
        return 5
    picked = _pick_visible(client, TARGET)
    if picked is None:
        return 2
    client._hunt_queue = [picked]
    client._blocked_screen_targets = []
    module = SamuraiCampsModule()
    deadline = time.time() + DEADLINE_SECONDS
    last = ""
    while time.time() < deadline:
        if not client._hunt_queue:
            picked = _pick_visible(client, TARGET)
            if picked is None:
                time.sleep(0.4)
                continue
            client._hunt_queue = [picked]
        last = module.run_cycle(client)
        logger.info("samurai cycle -> {}", last)
        if last in {"samurai", "client:1"} or str(last).startswith("client:"):
            logger.info("Тестовая атака отправлена: {}", TARGET)
            return 0
        if last in {"no_commanders", "samurai_complete"}:
            logger.error("Стоп: {}", last)
            return 3
        if last == "ruby_movement_refused":
            logger.error("Рубиновый конь выбран — отправка убита, перо не подтвердилось")
            return 4
        time.sleep(0.2)
    logger.error("За {}с отправки не было, последний результат: {}", DEADLINE_SECONDS, last)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
