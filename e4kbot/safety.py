from __future__ import annotations

import random
import time
from datetime import datetime
from typing import Any

from loguru import logger

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
    time.sleep(delay)
    return delay


def attack_gap_sleep(config: dict[str, Any]) -> float:
    return human_sleep(config.get("attack_delay_seconds") or [4, 10], "кд между атаками")


def cycle_pause_sleep(config: dict[str, Any]) -> float:
    return human_sleep(config.get("cycle_pause_seconds") or [12, 25], "пауза цикла")


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
        time.sleep(30)


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
