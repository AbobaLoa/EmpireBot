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

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# Official / wiki names for Empire / Empire: Four Kingdoms.
MODES: tuple[ModeSpec, ...] = (
    ModeSpec(
        id="robber_barons",
        title_ru="Замки разбойников",
        title_en="Robber Baron Castles",
        kingdom_ru="Великая империя",
        kingdom_en="The Great Empire",
        official_name="Robber Baron Castles",
        notes="Живой экранный сценарий. Ближайшая незакрытая цель к основному замку. Модуль robber_barons.",
        status="live",
        target_kind="baron",
        default_quota=20,
        priority=10,
    ),
    ModeSpec(
        id="storm_forts",
        title_ru="Форты островов ураганов",
        title_en="Storm Forts",
        kingdom_ru="Острова ураганов",
        kingdom_en="The Storm Islands",
        official_name="Storm Forts",
        notes="Заглушка. NPC-форты ивента Storm Islands, добыча aquamarine.",
        status="stub",
        target_kind="storm_fort",
        default_quota=10,
        priority=20,
    ),
    ModeSpec(
        id="barbarian_towers",
        title_ru="Варварские башни",
        title_en="Barbarian Towers",
        kingdom_ru="Вечный ледник",
        kingdom_en="Everwinter Glacier",
        official_name="Barbarian Towers",
        notes="Заглушка. Башни вечного ледника.",
        status="stub",
        target_kind="barbarian_tower",
        default_quota=10,
        priority=30,
    ),
    ModeSpec(
        id="barbarian_fortresses",
        title_ru="Варварские крепости",
        title_en="Barbarian Fortresses",
        kingdom_ru="Вечный ледник",
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
        title_ru="Башни пустыни",
        title_en="Desert Towers",
        kingdom_ru="Пылающие пески",
        kingdom_en="The Burning Sands",
        official_name="Desert Towers",
        notes="Заглушка. Башни пылающих песков.",
        status="stub",
        target_kind="desert_tower",
        default_quota=10,
        priority=40,
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
        title_ru="Башни культистов",
        title_en="Cultist Towers",
        kingdom_ru="Огненные вершины",
        kingdom_en="The Fire Peaks",
        official_name="Cultist Towers",
        notes="Заглушка. Массовые атаки на башни культистов.",
        status="stub",
        target_kind="cultist_tower",
        default_quota=15,
        priority=50,
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
        id="nomad_camps",
        title_ru="Лагеря кочевников",
        title_en="Nomad Camps",
        kingdom_ru="Великая империя",
        kingdom_en="The Great Empire",
        official_name="Nomad Invasion / Nomad Camps",
        notes="Заглушка. Ивент Nomad Invasion, не путать с ханским лагерем альянса.",
        status="stub",
        target_kind="nomad",
        default_quota=8,
        priority=60,
    ),
    ModeSpec(
        id="samurai_camps",
        title_ru="Лагеря самураев",
        title_en="Samurai Camps",
        kingdom_ru="Великая империя",
        kingdom_en="The Great Empire",
        official_name="Samurai Invasion / Samurai Camps",
        notes="Живой экранный сценарий Samurai Invasion: 4 лагеря × 11 атак, орудия и предустановки.",
        status="live",
        target_kind="samurai",
        default_quota=44,
        priority=61,
    ),
    ModeSpec(
        id="bloodcrows",
        title_ru="Стервятники",
        title_en="Bloodcrows",
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
        title_ru="Замки чужаков",
        title_en="Alien Castles",
        kingdom_ru="Великая империя",
        kingdom_en="The Great Empire",
        official_name="Alien Invasion / Alien Castles",
        notes="Заглушка. Ивент Alien Invasion, чужие замки.",
        status="stub",
        target_kind="alien",
        default_quota=6,
        priority=71,
    ),
)

MODE_BY_ID = {mode.id: mode for mode in MODES}


def default_campaign_queue() -> list[dict[str, Any]]:
    return [
        {
            "mode": mode.id,
            "count": mode.default_quota,
            "enabled": mode.status == "live",
        }
        for mode in MODES
    ]


def catalog_payload() -> list[dict[str, Any]]:
    return [mode.to_dict() for mode in MODES]
