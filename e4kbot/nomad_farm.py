"""Nomad camp farming: 4 physical camps, 11 hits each, levels n..n+10 on the same yurt."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from e4kbot.state import NOMAD_HITS_PER_CAMP

NOMAD_DEFAULT_NUM_CAMPS = 4
NOMAD_DEFAULT_START_LEVEL = 41
NOMAD_DEFAULT_COOLDOWN_HOURS = 1.5


@dataclass(frozen=True)
class NomadFarmSettings:
    """Configuration for nomad invasion farming."""

    start_level: int = NOMAD_DEFAULT_START_LEVEL
    max_attacks_per_camp: int = NOMAD_HITS_PER_CAMP
    camp_cooldown_hours: float = NOMAD_DEFAULT_COOLDOWN_HOURS
    num_camps: int = NOMAD_DEFAULT_NUM_CAMPS

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> NomadFarmSettings:
        raw = config.get("nomad_farm") or {}
        start = int(raw.get("start_level", NOMAD_DEFAULT_START_LEVEL))
        max_attacks = int(raw.get("max_attacks_per_camp", NOMAD_HITS_PER_CAMP))
        cooldown = float(raw.get("camp_cooldown_hours", NOMAD_DEFAULT_COOLDOWN_HOURS))
        num_camps = int(raw.get("num_camps", NOMAD_DEFAULT_NUM_CAMPS))
        return cls(
            start_level=max(1, min(99, start)),
            max_attacks_per_camp=max(1, min(99, max_attacks)),
            camp_cooldown_hours=max(0.1, min(24.0, cooldown)),
            num_camps=max(1, min(10, num_camps)),
        )

    @property
    def end_level(self) -> int:
        """Highest level reached on one camp before cooldown (n+10 for 11 attacks)."""
        return self.start_level + self.max_attacks_per_camp - 2

    @property
    def camp_cooldown_sec(self) -> float:
        return self.camp_cooldown_hours * 3600.0

    def level_after_attacks(self, attacks_done: int) -> int:
        """Expected visible camp level after `attacks_done` successful sends."""
        if attacks_done <= 0:
            return self.start_level
        return min(self.start_level + attacks_done, self.end_level)

    def attacks_remaining(self, attacks_done: int) -> int:
        return max(0, self.max_attacks_per_camp - int(attacks_done))


def camp_key(coords: tuple[int, int] | list[int]) -> str:
    return f"{int(coords[0])}:{int(coords[1])}"


@dataclass
class NomadCampRecord:
    """Per-camp progress on the map (same physical yurt, 11 hits)."""

    coords: list[int]
    attacks_done: int = 0
    current_level: int | None = None
    cooldown_until: float = 0.0

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None, key: str) -> NomadCampRecord:
        if not raw:
            parts = key.split(":")
            coords = [int(parts[0]), int(parts[1])] if len(parts) == 2 else [0, 0]
            return cls(coords=coords)
        coords = raw.get("coords")
        if not isinstance(coords, (list, tuple)) or len(coords) != 2:
            parts = key.split(":")
            coords = [int(parts[0]), int(parts[1])] if len(parts) == 2 else [0, 0]
        return cls(
            coords=[int(coords[0]), int(coords[1])],
            attacks_done=int(raw.get("attacks_done") or 0),
            current_level=(
                int(raw["current_level"]) if raw.get("current_level") is not None else None
            ),
            cooldown_until=float(raw.get("cooldown_until") or 0.0),
        )

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "coords": [int(self.coords[0]), int(self.coords[1])],
            "attacks_done": int(self.attacks_done),
            "cooldown_until": float(self.cooldown_until),
        }
        if self.current_level is not None:
            payload["current_level"] = int(self.current_level)
        return payload


@dataclass
class NomadFarmProgress:
    """Tracks up to 4 physical nomad camps and rotation between them."""

    camps: dict[str, dict[str, Any]] = field(default_factory=dict)
    active_camp: str | None = None

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> NomadFarmProgress:
        if not raw:
            return cls()
        legacy_index = raw.get("level_index")
        if legacy_index is not None and not raw.get("camps"):
            return cls()
        camps = raw.get("camps") or {}
        if not isinstance(camps, dict):
            camps = {}
        return cls(
            camps={str(key): dict(value) for key, value in camps.items()},
            active_camp=str(raw["active_camp"]) if raw.get("active_camp") else None,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "camps": {key: dict(value) for key, value in self.camps.items()},
            "active_camp": self.active_camp,
        }

    def get_camp(self, coords: tuple[int, int], settings: NomadFarmSettings) -> NomadCampRecord:
        key = camp_key(coords)
        record = NomadCampRecord.from_dict(self.camps.get(key), key)
        record.coords = [int(coords[0]), int(coords[1])]
        if record.current_level is None:
            record.current_level = settings.level_after_attacks(record.attacks_done)
        return record

    def save_camp(self, record: NomadCampRecord) -> None:
        key = camp_key(record.coords)
        self.camps[key] = record.to_dict()

    def camp_on_cooldown(self, coords: tuple[int, int], now: float | None = None) -> bool:
        record = self.get_camp(coords, NomadFarmSettings())
        until = float(record.cooldown_until or 0.0)
        return until > (now or time.time())

    def camp_finished(self, coords: tuple[int, int], settings: NomadFarmSettings) -> bool:
        record = self.get_camp(coords, settings)
        return int(record.attacks_done) >= settings.max_attacks_per_camp

    def camp_available(
        self,
        coords: tuple[int, int],
        settings: NomadFarmSettings,
        now: float | None = None,
    ) -> bool:
        now = now or time.time()
        record = self.get_camp(coords, settings)
        if int(record.attacks_done) < settings.max_attacks_per_camp:
            return True
        return float(record.cooldown_until or 0.0) <= now

    def reset_camp_if_cooldown_expired(
        self,
        coords: tuple[int, int],
        settings: NomadFarmSettings,
        now: float | None = None,
    ) -> bool:
        """Clear a finished camp after cooldown so it can be farmed again."""
        now = now or time.time()
        record = self.get_camp(coords, settings)
        if int(record.attacks_done) < settings.max_attacks_per_camp:
            return False
        if float(record.cooldown_until or 0.0) > now:
            return False
        record.attacks_done = 0
        record.current_level = settings.start_level
        record.cooldown_until = 0.0
        self.save_camp(record)
        if self.active_camp == camp_key(coords):
            self.active_camp = None
        return True

    def record_attack(
        self,
        coords: tuple[int, int],
        settings: NomadFarmSettings,
        ocr_level: int | None = None,
        now: float | None = None,
    ) -> NomadCampRecord:
        now = now or time.time()
        record = self.get_camp(coords, settings)
        record.attacks_done = int(record.attacks_done) + 1
        if ocr_level is not None:
            record.current_level = int(ocr_level)
        else:
            record.current_level = settings.level_after_attacks(record.attacks_done)
        if record.attacks_done >= settings.max_attacks_per_camp:
            record.cooldown_until = now + settings.camp_cooldown_sec
            self.active_camp = None
        else:
            self.active_camp = camp_key(coords)
        self.save_camp(record)
        return record

    def sync_from_store_hits(
        self,
        coords: tuple[int, int],
        hits: int,
        settings: NomadFarmSettings,
    ) -> NomadCampRecord:
        record = self.get_camp(coords, settings)
        if int(hits) > int(record.attacks_done):
            record.attacks_done = int(hits)
            record.current_level = settings.level_after_attacks(record.attacks_done)
            self.save_camp(record)
        return record

    def pick_next_camp(
        self,
        candidates: list[tuple[int, int]],
        settings: NomadFarmSettings,
        now: float | None = None,
    ) -> tuple[int, int] | None:
        now = now or time.time()
        for coords in candidates:
            self.reset_camp_if_cooldown_expired(coords, settings, now)
        if self.active_camp:
            for coords in candidates:
                if camp_key(coords) == self.active_camp and self.camp_available(coords, settings, now):
                    if not self.camp_finished(coords, settings):
                        return coords
        for coords in candidates:
            if self.camp_available(coords, settings, now) and not self.camp_finished(coords, settings):
                self.active_camp = camp_key(coords)
                return coords
        return None

    def status_line(
        self,
        settings: NomadFarmSettings,
        coords: tuple[int, int] | None = None,
    ) -> str:
        if coords is None and self.active_camp:
            parts = self.active_camp.split(":")
            if len(parts) == 2:
                coords = (int(parts[0]), int(parts[1]))
        if coords is None:
            tracked = len(self.camps)
            return (
                f"кочевники: {settings.num_camps} лагеря, "
                f"ур. {settings.start_level}–{settings.end_level}, "
                f"до {settings.max_attacks_per_camp} атак"
            )
        record = self.get_camp(coords, settings)
        level = record.current_level or settings.level_after_attacks(record.attacks_done)
        done = int(record.attacks_done)
        total = settings.max_attacks_per_camp
        if self.camp_on_cooldown(coords):
            left = max(0, int(record.cooldown_until - time.time()))
            return f"лагерь {coords} ур.{level}: CD {left // 60}м"
        return f"лагерь {coords} ур.{level}: атака {done + 1}/{total}"

    def reset(self) -> None:
        self.camps.clear()
        self.active_camp = None
