from __future__ import annotations

import random
import time
from datetime import datetime
from typing import Any

from loguru import logger

from e4kbot.control import CONTROL


MAX_COMMANDER_NUMBER = 30
MAX_CONCURRENT_ATTACKS = 30


def triangular_delay(span: list | tuple | float | int) -> float:
    if isinstance(span, (list, tuple)) and len(span) >= 2:
        lo, hi = float(span[0]), float(span[1])
        if lo > hi:
            lo, hi = hi, lo
        if lo == hi:
            return lo
        return random.triangular(lo, hi, (lo + hi) / 2)
    return float(span)


def human_sleep(span: list | tuple | float | int, reason: str = "") -> float:
    delay = max(0.0, triangular_delay(span))
    if reason:
        logger.debug(f"пауза {delay:.1f}с ({reason})")
    CONTROL.sleep(delay)
    return delay


def attack_gap_sleep(config: dict[str, Any]) -> float:
    return human_sleep(config.get("attack_delay_seconds") or [8, 10], "кд между атаками")


def cycle_pause_sleep(config: dict[str, Any]) -> float:
    span = config.get("cycle_pause_seconds") or [0, 0]
    if isinstance(span, (list, tuple)) and max(float(span[0]), float(span[1])) <= 0:
        return 0.0
    return human_sleep(span, "пауза цикла")


def send_cadence_span(config: dict[str, Any] | None = None) -> tuple[float, float]:
    span = (config or {}).get("attack_delay_seconds") or [8, 10]
    if isinstance(span, (list, tuple)) and len(span) >= 2:
        lo, hi = float(span[0]), float(span[1])
        return (hi, lo) if lo > hi else (lo, hi)
    value = float(span)
    return value, value


def roll_send_gap(config: dict[str, Any] | None = None) -> float:
    lo, hi = send_cadence_span(config)
    if lo == hi:
        return lo
    return random.uniform(lo, hi)


def send_gap_remaining(last_send_at: float | None, now: float, gap: float) -> float:
    """Seconds to wait so send happens at last_send_at+gap. Late formation waits 0."""
    if not last_send_at or last_send_at <= 0 or gap <= 0:
        return 0.0
    return max(0.0, float(last_send_at) + float(gap) - float(now))


def wait_for_send_slot(store: Any, config: dict[str, Any] | None = None) -> float:
    """Block only the leftover send-to-send window, right before the final confirm."""
    remaining = max(0.0, float(getattr(store.live, "next_send_at", 0) or 0) - time.time())
    if remaining <= 0:
        return 0.0
    logger.info("Жду слот отправки {:.1f}с (каденс 8–10с от прошлой атаки)", remaining)
    CONTROL.sleep(remaining)
    return remaining


def mark_successful_send(store: Any, config: dict[str, Any] | None = None, now: float | None = None) -> float:
    sent = time.time() if now is None else float(now)
    gap = roll_send_gap(config)
    store.live.last_attack_at = sent
    store.live.next_send_at = sent + gap
    logger.info("Следующая отправка не раньше чем через {:.1f}с", gap)
    return gap


def tap_jitter(px: int = 6) -> tuple[int, int]:
    return random.randint(-px, px), random.randint(-px, px)


def is_within_active_hours(config: dict[str, Any]) -> bool:
    hours = config.get("active_hours") or [0, 24]
    if not isinstance(hours, (list, tuple)) or len(hours) < 2:
        return True
    start, end = int(hours[0]), int(hours[1])
    hour = datetime.now().hour
    if start == end:
        return True
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


def wait_active_hours(config: dict[str, Any]) -> None:
    if is_within_active_hours(config):
        return
    hours = config.get("active_hours") or [8, 24]
    logger.info(f"Вне активных часов ({hours[0]}–{hours[1]}), ждём")
    while not is_within_active_hours(config):
        CONTROL.sleep(30)


def commander_number_ok(number: int | None, config: dict[str, Any]) -> tuple[bool, str]:
    if number is None:
        return True, ""
    cap = int(config.get("max_commander_number") or MAX_COMMANDER_NUMBER)
    if int(number) > cap:
        return False, (
            f"Номер военачальника {number} > {cap}. Атаки остановлены."
        )
    return True, ""


def concurrent_ok(in_flight: int, config: dict[str, Any]) -> tuple[bool, str]:
    cap = int(config.get("max_concurrent_attacks") or MAX_CONCURRENT_ATTACKS)
    if in_flight >= cap:
        return False, f"Уже {in_flight} атак в пути (лимит {cap})"
    return True, ""
