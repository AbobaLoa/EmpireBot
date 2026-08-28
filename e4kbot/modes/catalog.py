from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Literal

ModeStatus = Literal["live", "stub"]


@dataclass(frozen=True)
class ModeSpec:
    id: str
    title_ru: str
    title_en: str
    kingdom_ru: str
    kingdom_en: str
    official_name: str
    notes: str
    status: ModeStatus
    target_kind: str
    default_quota: int
    priority: int
    campaign_priority: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# Official / wiki names for Empire / Empire: Four Kingdoms.
MODES: tuple[ModeSpec, ...] = (
    ModeSpec(
        id="robber_barons",
        title_ru="Замки баронов",
        title_en="Robber Baron Castles",
        kingdom_ru="Великая империя",
        kingdom_en="The Great Empire",
        official_name="Robber Baron Castles",
        notes="Живой экранный сценарий. Ближайшая незакрытая цель к основному замку. Модуль robber_barons.",
        status="live",
        target_kind="baron",
        default_quota=5,
        priority=30,
        campaign_priority=3,
    ),
    ModeSpec(
        id="barbarian_towers",
        title_ru="Варварская башня",
        title_en="Barbarian Towers",
        kingdom_ru="Вечнохолодный ледник",
        kingdom_en="Everwinter Glacier",
        official_name="Barbarian Towers",
        notes="Живой сценарий. Навигация → левый секстант. Волны 100%. Тур крутится, пока не выключат.",
        status="live",
        target_kind="barbarian_tower",
        default_quota=5,
        priority=31,
        campaign_priority=3,
    ),
    ModeSpec(
        id="barbarian_fortresses",
        title_ru="Варварские крепости",
        title_en="Barbarian Fortresses",
        kingdom_ru="Вечнохолодный ледник",
        kingdom_en="Everwinter Glacier",
        official_name="Barbarian Fortresses",
        notes="Заглушка. Поиск и атака неатакованных крепостей ледника.",
        status="stub",
        target_kind="barbarian_fortress",
        default_quota=5,
        priority=31,
    ),
    ModeSpec(
        id="desert_towers",
        title_ru="Башня в пустыне",
        title_en="Desert Towers",
        kingdom_ru="Пылающие пески",
        kingdom_en="The Burning Sands",
        official_name="Desert Towers",
        notes="Живой сценарий. Навигация → левый секстант. Волны 100%. Тур крутится, пока не выключат.",
        status="live",
        target_kind="desert_tower",
        default_quota=5,
        priority=32,
        campaign_priority=3,
    ),
    ModeSpec(
        id="desert_fortresses",
        title_ru="Крепости пустыни",
        title_en="Desert Fortresses",
        kingdom_ru="Пылающие пески",
        kingdom_en="The Burning Sands",
        official_name="Desert Fortresses",
        notes="Заглушка. Приоритетный поиск и атака крепостей пустыни.",
        status="stub",
        target_kind="desert_fortress",
        default_quota=5,
        priority=41,
    ),
    ModeSpec(
        id="cultist_towers",
        title_ru="Башня культистов",
        title_en="Cultist Towers",
        kingdom_ru="Огненные вершины",
        kingdom_en="The Fire Peaks",
        official_name="Cultist Towers",
        notes="Живой сценарий. Навигация → левый секстант. Волны 100%. Тур крутится, пока не выключат.",
        status="live",
        target_kind="cultist_tower",
        default_quota=5,
        priority=33,
        campaign_priority=3,
    ),
    ModeSpec(
        id="dragons",
        title_ru="Драконы",
        title_en="Dragons",
        kingdom_ru="Огненные вершины",
        kingdom_en="The Fire Peaks",
        official_name="Dragons",
        notes="Заглушка. Поиск и атака дракона на огненных вершинах.",
        status="stub",
        target_kind="dragon",
        default_quota=5,
        priority=51,
    ),
    ModeSpec(
        id="storm_forts",
        title_ru="Форты ураганов",
        title_en="Storm Forts",
        kingdom_ru="Острова ураганов",
        kingdom_en="The Storm Islands",
        official_name="Storm Forts",
        notes="Живой сценарий. Только центр, две волны по 100%. Если мира нет в Навигации — отчёт и дальше. Тур крутится, пока не выключат.",
        status="live",
        target_kind="storm_fort",
        default_quota=5,
        priority=34,
        campaign_priority=3,
    ),
    ModeSpec(
        id="nomad_camps",
        title_ru="Вторжение кочевников",
        title_en="Nomad Invasion",
        kingdom_ru="Великая империя",
        kingdom_en="The Great Empire",
        official_name="Nomad Invasion / Nomad Camps",
        notes="Живой экранный сценарий Nomad Invasion: 4 лагеря, орудия с биркой и предустановки.",
        status="live",
        target_kind="nomad",
        default_quota=44,
        priority=10,
        campaign_priority=1,
    ),
    ModeSpec(
        id="samurai_camps",
        title_ru="Нашествие самураев",
        title_en="Samurai Invasion",
        kingdom_ru="Великая империя",
        kingdom_en="The Great Empire",
        official_name="Samurai Invasion / Samurai Camps",
        notes="Живой экранный сценарий Samurai Invasion: 4 лагеря × 11 атак, орудия и предустановки.",
        status="live",
        target_kind="samurai",
        default_quota=44,
        priority=11,
        campaign_priority=1,
    ),
    ModeSpec(
        id="bloodcrows",
        title_ru="Вторжение стервятников",
        title_en="Bloodcrow Invasion",
        kingdom_ru="Великая империя",
        kingdom_en="The Great Empire",
        official_name="Bloodcrow Invasion",
        notes="Заглушка. Ивент Bloodcrow Invasion; в RU-сообществе часто «стервятники».",
        status="stub",
        target_kind="bloodcrow",
        default_quota=6,
        priority=70,
    ),
    ModeSpec(
        id="alien_castles",
        title_ru="Вторжение чужеземцев",
        title_en="Alien Invasion",
        kingdom_ru="Великая империя",
        kingdom_en="The Great Empire",
        official_name="Alien Invasion / Alien Castles",
        notes="Заглушка. Официально Alien Invasion (замки чужаков). В RU-клиенте часто «вторжение чужеземцев».",
        status="stub",
        target_kind="alien",
        default_quota=6,
        priority=40,
        campaign_priority=4,
    ),
)

MODE_BY_ID = {mode.id: mode for mode in MODES}

KIND_TO_MODE = {mode.target_kind: mode.id for mode in MODES}

# Four kingdoms plus Fire Peaks (catalog order for the control panel).
KINGDOM_ORDER: tuple[str, ...] = (
    "Великая империя",
    "Вечнохолодный ледник",
    "Пылающие пески",
    "Огненные вершины",
    "Острова ураганов",
)


def campaign_sort_key(mode: ModeSpec) -> tuple[int, str]:
    """P1 events, then GE → glacier → sands → peaks → storm, then P4 aliens."""
    rank = {
        "nomad_camps": 10,
        "samurai_camps": 11,
        "robber_barons": 30,
        "barbarian_towers": 31,
        "desert_towers": 32,
        "cultist_towers": 33,
        "storm_forts": 34,
        "alien_castles": 40,
    }
    return (rank.get(mode.id, 90 + int(mode.priority)), mode.id)


def ordered_mode_specs() -> tuple[ModeSpec, ...]:
    return tuple(sorted(MODES, key=campaign_sort_key))


def default_campaign_queue() -> list[dict[str, Any]]:
    auto_on = {"robber_barons", "nomad_camps", "samurai_camps"}
    return [
        {
            "mode": mode.id,
            "count": mode.default_quota,
            "enabled": mode.id in auto_on,
        }
        for mode in ordered_mode_specs()
    ]


def catalog_payload() -> list[dict[str, Any]]:
    return [mode.to_dict() for mode in MODES]


def catalog_grouped() -> list[dict[str, Any]]:
    by_kingdom: dict[str, list[ModeSpec]] = {}
    for mode in MODES:
        by_kingdom.setdefault(mode.kingdom_ru, []).append(mode)
    groups: list[dict[str, Any]] = []
    seen: set[str] = set()
    for name in KINGDOM_ORDER:
        modes = by_kingdom.get(name) or []
        if not modes:
            continue
        seen.add(name)
        groups.append(
            {
                "kingdom_ru": name,
                "kingdom_en": modes[0].kingdom_en,
                "modes": [mode.to_dict() for mode in modes],
            }
        )
    for name, modes in by_kingdom.items():
        if name in seen:
            continue
        groups.append(
            {
                "kingdom_ru": name,
                "kingdom_en": modes[0].kingdom_en,
                "modes": [mode.to_dict() for mode in modes],
            }
        )
    return groups
