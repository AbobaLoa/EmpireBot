"""Nomad camp farming: level sequence 40–50 (11 camps), 11 hits per camp via state."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from e4kbot.state import NOMAD_HITS_PER_CAMP


@dataclass(frozen=True)
class NomadFarmSettings:
    start_level: int = 40
    end_level: int = 50
    max_attacks_per_camp: int = NOMAD_HITS_PER_CAMP

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> NomadFarmSettings:
        raw = config.get("nomad_farm") or {}
        start = int(raw.get("start_level", 40))
        end = int(raw.get("end_level", 50))
        max_attacks = int(raw.get("max_attacks_per_camp", NOMAD_HITS_PER_CAMP))
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
    """Index into the configured level sequence (0 = first camp level)."""

    level_index: int = 0
    cleared_levels: list[int] = field(default_factory=list)

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> NomadProgress:
        if not raw:
            return cls()
        cleared = raw.get("cleared_levels") or raw.get("attacks_by_level") or []
        if isinstance(cleared, dict):
            cleared = [int(k) for k, v in cleared.items() if int(v) >= NOMAD_HITS_PER_CAMP]
        return cls(
            level_index=int(raw.get("level_index") or 0),
            cleared_levels=[int(value) for value in cleared],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "level_index": int(self.level_index),
            "cleared_levels": [int(value) for value in self.cleared_levels],
        }

    def current_level(self, settings: NomadFarmSettings) -> int | None:
        levels = settings.levels
        if not levels or self.level_index >= len(levels):
            return None
        return levels[self.level_index]

    def is_complete(self, settings: NomadFarmSettings) -> bool:
        return self.level_index >= len(settings.levels)

    def advance_after_camp(self, settings: NomadFarmSettings, level: int) -> None:
        level = int(level)
        if level not in self.cleared_levels:
            self.cleared_levels.append(level)
        if self.level_index < len(settings.levels) and settings.levels[self.level_index] == level:
            self.level_index += 1

    def reset(self) -> None:
        self.level_index = 0
        self.cleared_levels.clear()

    def status_line(self, settings: NomadFarmSettings) -> str:
        current = self.current_level(settings)
        if current is None:
            return f"кочевники {settings.start_level}–{settings.end_level}: цикл завершён"
        idx = self.level_index + 1
        total = settings.camp_count
        return f"лагерь ур. {current} ({idx}/{total}), до {settings.max_attacks_per_camp} атак"
