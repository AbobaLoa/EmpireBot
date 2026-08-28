"""Send 5 robber-baron attacks at max speed and print wall-clock + pack span."""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from loguru import logger

from e4kbot.attacks.robber_barons import RobberBaronsModule
from e4kbot.bluestacks import AdbClient
from e4kbot.client import BlueStacksEngine
from e4kbot.config import load_config
from e4kbot.control import CONTROL
from e4kbot.paths import LOG_DIR, ensure_dirs
from e4kbot.state import StateStore
from e4kbot.telegram_bot import TelegramReporter
from e4kbot.vision import (
    find_travel_seal_pair,
    is_formation_screen,
    is_start_attack_gate,
    is_travel_dialog,
)

SEND_TARGET = 5
DEADLINE_SECONDS = 180


def _send_target() -> int:
    if len(sys.argv) > 1:
        return max(1, int(sys.argv[1]))
    return SEND_TARGET


def _close_leftover_plan(client: BlueStacksEngine) -> None:
    image = client._image()
    if is_travel_dialog(image):
        pair = find_travel_seal_pair(image)
        if pair is not None:
            _green, red = pair
            client._tap_norm_exact(*red)
        else:
            client._tap_norm_exact(0.23, 0.815)
        CONTROL.sleep(0.35)
        return
    if is_formation_screen(image) or is_start_attack_gate(image):
        logger.info("Закрываю чужой план (самураи) перед пачкой баронов")
        client.close_formation_plan()
        CONTROL.sleep(0.35)


def main() -> int:
    ensure_dirs()
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
    CONTROL.configure(config, startup=True)
    CONTROL.enable()
    store = StateStore()
    store.live.dry_run = False
    store.live.mode = "attack"
    store.live.active_mode = "robber_barons"
    store.live.next_send_at = 0
    store.live.next_attack_at = 0
    store.save()
    telegram = TelegramReporter(config)
    adb = AdbClient(config)
    try:
        adb.connect()
    except Exception:
        logger.warning("ADB нет — клики мышью")
    client = BlueStacksEngine(config, store, telegram, adb)
    client._qualifying_cycles = 0
    client._speed_burst_done = False
    client._burst_send_times = []
    client._pack_finished = False
    client._pack_send_times = []
    client._pack_prep_started_at = None
    client._need_ge_home = True
    client._switched_world_id = None
    want = _send_target()
    if not client._speed_burst_active():
        logger.error("Скоростной режим не включился — qualifying_cycles={}", client._qualifying_cycles)
        return 3
    logger.warning("SPEED ON — пачка {} баронов, засекаю время", want)
    _close_leftover_plan(client)
    pack_close: list[tuple[str, float, bool]] = []
    orig_close = client._close_world_pack
    orig_note = client._note_pack_send

    def _capture_close(mode: str, span: float, ok: bool) -> None:
        pack_close.append((mode, float(span), bool(ok)))
        orig_close(mode, span, ok)

    def _note_and_stop(kind: str) -> None:
        orig_note(kind)
        times = getattr(client, "_pack_send_times", None) or []
        if len(times) >= want:
            span = times[-1] - float(client._pack_prep_started_at or times[0])
            pack_close.append(("robber_barons", float(span), span <= 40.0))
            client._pack_finished = True
            logger.warning("TEST STOP after {}/{} sends span={:.2f}s", len(times), want, span)

    client._close_world_pack = _capture_close  # type: ignore[method-assign]
    client._note_pack_send = _note_and_stop  # type: ignore[method-assign]
    module = RobberBaronsModule()
    wall_started = time.perf_counter()
    deadline = time.time() + DEADLINE_SECONDS
    last = ""
    while time.time() < deadline:
        if pack_close or getattr(client, "_pack_finished", False):
            break
        last = module.run_cycle(client)
        sends_now = len(getattr(client, "_pack_send_times", None) or [])
        logger.info("baron cycle -> {} live_sends={}", last, sends_now)
        if last == "no_commanders":
            logger.error("Нет военачальников")
            break
        if last in {"no_targets", "world_skip_empty"}:
            CONTROL.sleep(0.2)
            continue
    elapsed = time.perf_counter() - wall_started
    pack_span = pack_close[-1][1] if pack_close else None
    pack_ok = pack_close[-1][2] if pack_close else None
    live_sends = len(getattr(client, "_pack_send_times", None) or [])
    sends = want if pack_close else live_sends
    logger.warning(
        "BARON PACK TIMING wall={:.2f}s sends={}/{} pack_span={} ok={} last={}",
        elapsed,
        sends,
        want,
        f"{float(pack_span):.2f}s" if pack_span is not None else "n/a",
        pack_ok,
        last,
    )
    print(
        f"BARON_PACK sends={sends}/{want} "
        f"wall={elapsed:.2f}s pack_span={pack_span if pack_span is not None else 'n/a'} "
        f"ok={pack_ok} last={last}",
        flush=True,
    )
    if sends >= want:
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
