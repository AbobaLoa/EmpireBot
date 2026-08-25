"""Nomad camp farming: level progression and per-camp attack limits."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class NomadFarmSettings:
    start_level: int = 40
    end_level: int = 50
    max_attacks_per_camp: int = 11

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> NomadFarmSettings:
        raw = config.get("nomad_farm") or {}
        start = int(raw.get("start_level", 40))
        end = int(raw.get("end_level", 50))
        max_attacks = int(raw.get("max_attacks_per_camp", 11))
        return cls(
            start_level=max(1, min(99, start)),
            end_level=max(1, min(99, end)),
            max_attacks_per_camp=max(1, min(99, max_attacks)),
        )

    @property
    def levels(self) -> list[int]:
        if self.end_level < self.start_level:
            return []
        return list(range(self.start_level, self.end_level + 1))

    @property
    def camp_count(self) -> int:
        return len(self.levels)


@dataclass
class NomadProgress:
    level_index: int = 0
    attacks_by_level: dict[int, int] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> NomadProgress:
        if not raw:
            return cls()
        attacks_raw = raw.get("attacks_by_level") or {}
        attacks = {int(key): int(value) for key, value in attacks_raw.items()}
        return cls(level_index=int(raw.get("level_index") or 0), attacks_by_level=attacks)

    def to_dict(self) -> dict[str, Any]:
        return {
            "level_index": int(self.level_index),
            "attacks_by_level": {
                str(level): int(count) for level, count in sorted(self.attacks_by_level.items())
            },
        }

    def current_level(self, settings: NomadFarmSettings) -> int | None:
        levels = settings.levels
        if not levels or self.level_index >= len(levels):
            return None
        return levels[self.level_index]

    def attacks_on(self, level: int) -> int:
        return int(self.attacks_by_level.get(int(level), 0))

    def remaining_on_level(self, settings: NomadFarmSettings, level: int) -> int:
        return max(0, settings.max_attacks_per_camp - self.attacks_on(level))

    def record_success(self, settings: NomadFarmSettings, level: int) -> bool:
        """Register one successful attack. Returns True if the camp level is finished."""
        level = int(level)
        self.attacks_by_level[level] = self.attacks_on(level) + 1
        finished = self.attacks_by_level[level] >= settings.max_attacks_per_camp
        if finished:
            self.level_index += 1
        return finished

    def is_complete(self, settings: NomadFarmSettings) -> bool:
        return self.level_index >= len(settings.levels)

    def reset(self) -> None:
        self.level_index = 0
        self.attacks_by_level.clear()

    def status_line(self, settings: NomadFarmSettings) -> str:
        current = self.current_level(settings)
        if current is None:
            return f"кочевники {settings.start_level}–{settings.end_level}: цикл завершён"
        idx = self.level_index + 1
        total = settings.camp_count
        attacks = self.attacks_on(current)
        return (
            f"лагерь {current} ({idx}/{total}), "
            f"атака {attacks + 1}/{settings.max_attacks_per_camp}"
        )
