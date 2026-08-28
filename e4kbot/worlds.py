from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from e4kbot.paths import DATA_DIR


def _ocr_compact(text: str) -> str:
    return "".join(ch for ch in (text or "").lower().replace("ё", "е") if ch.isalnum())

STORM_UNOPENED_LINE = "острова ураганов не открыты"
CONTINUE_ONE_WORLD_LINE = "такие миры не открыты, продолжаю в открытых"

WORLD_NPC_KINDS = frozenset(
    {
        "baron",
        "barbarian_tower",
        "desert_tower",
        "cultist_tower",
        "storm_fort",
    }
)


@dataclass(frozen=True)
class WorldSpec:
    id: str
    kingdom_id: int
    display_ru: str
    display_en: str
    ocr_needles: tuple[str, ...]
    target_kind: str
    mode_id: str
    target_needles: tuple[str, ...]
    attacks: int = 5
    fill_ratio: float = 1.0
    waves: int = 1
    tour: bool = True
    skip_if_unopened: bool = True
    skip_if_first_cannot_fill: bool = False
    wait_last_if_cannot_fill: bool = True
    center_only: bool = True


WORLDS: tuple[WorldSpec, ...] = (
    WorldSpec(
        id="great_empire",
        kingdom_id=0,
        display_ru="Великая империя",
        display_en="The Great Empire",
        ocr_needles=(
            "великаяимпер",
            "velikayaimper",
            "greatempire",
            "benukaa",
            "benukan",
            "benukxa",
            "umnep",
            "vimnep",
            "vmnep",
        ),
        target_kind="baron",
        mode_id="robber_barons",
        target_needles=("барон", "baron", "разбой", "robber"),
        attacks=5,
        fill_ratio=1.0,
        tour=True,
        skip_if_unopened=False,
        skip_if_first_cannot_fill=False,
        wait_last_if_cannot_fill=False,
    ),
    WorldSpec(
        id="everwinter",
        kingdom_id=1,
        display_ru="Вечнохолодный ледник",
        display_en="Everwinter Glacier",
        ocr_needles=(
            "вечнохолодн",
            "вечнохолдн",
            "вечныйледник",
            "ледник",
            "everwinter",
            "glacier",
            "beynoxonog",
            "beyhoxonog",
            "jleahuk",
            "jlequuk",
            "leahuk",
            "lequuk",
            "мороз",
            "mopo3",
            "moroz",
        ),
        target_kind="barbarian_tower",
        mode_id="barbarian_towers",
        target_needles=(
            "варварск",
            "варварскаябашн",
            "башня",
            "varvar",
            "bashn",
            "barbarian",
        ),
        skip_if_first_cannot_fill=False,
        wait_last_if_cannot_fill=True,
    ),
    WorldSpec(
        id="burning_sands",
        kingdom_id=2,
        display_ru="Пылающие пески",
        display_en="The Burning Sands",
        ocr_needles=(
            "пылающ",
            "пылающиепеск",
            "пески",
            "burningsand",
            "desert",
            "neinatoune",
            "mbinarouw",
            "necku",
            "mecku",
            "pyla",
        ),
        target_kind="desert_tower",
        mode_id="desert_towers",
        target_needles=(
            "пустын",
            "башнявпуст",
            "башня",
            "pustyn",
            "desert",
            "bashn",
        ),
    ),
    WorldSpec(
        id="fire_peaks",
        kingdom_id=3,
        display_ru="Огненные вершины",
        display_en="The Fire Peaks",
        ocr_needles=(
            "огненн",
            "огненые",
            "вершин",
            "firepeak",
            "orhehh",
            "bepwn",
            "bepwin",
            "vershin",
            "ognen",
            "драго",
            "aparo",
            "drago",
        ),
        target_kind="cultist_tower",
        mode_id="cultist_towers",
        target_needles=(
            "культист",
            "башнякульт",
            "cultist",
            "kultist",
            "bashn",
            "башня",
        ),
    ),
    WorldSpec(
        id="storm_islands",
        kingdom_id=4,
        display_ru="Острова ураганов",
        display_en="The Storm Islands",
        ocr_needles=(
            "остров",
            "ураган",
            "ostrov",
            "uragan",
            "yparan",
            "yparah",
            "octposa",
            "stormisland",
            "stormfort",
            "storm fort",
            "ураганов",
        ),
        target_kind="storm_fort",
        mode_id="storm_forts",
        target_needles=(
            "форт",
            "ураган",
            "fort",
            "uragan",
            "storm",
        ),
        waves=2,
        skip_if_first_cannot_fill=False,
        wait_last_if_cannot_fill=True,
    ),
)

WORLD_BY_ID = {item.id: item for item in WORLDS}
WORLD_BY_KIND = {item.target_kind: item for item in WORLDS}
WORLD_BY_MODE = {item.mode_id: item for item in WORLDS}
TOUR_WORLDS = tuple(item for item in WORLDS if item.tour)


@dataclass
class NavRow:
    world_id: str | None
    world_ru: str
    castle: str
    coords: tuple[int, int] | None
    sextant: tuple[float, float]
    blob: str


@dataclass
class NavigationScan:
    rows: list[NavRow] = field(default_factory=list)
    open_world_ids: list[str] = field(default_factory=list)
    missing_world_ids: list[str] = field(default_factory=list)
    blob: str = ""

    def row_for_world(self, world_id: str) -> NavRow | None:
        for row in self.rows:
            if row.world_id == world_id:
                return row
        spec = WORLD_BY_ID.get(world_id)
        if spec is None:
            return None
        for row in self.rows:
            if blob_hits(row.blob, spec.ocr_needles):
                return row
        return None


def compact_ui(text: str) -> str:
    return _ocr_compact(text)


def blob_hits(blob: str, needles: tuple[str, ...]) -> bool:
    hay = compact_ui(blob)
    if not hay:
        return False
    return any(token in hay for token in needles if token)


def match_world_id(text: str) -> str | None:
    hay = compact_ui(text)
    if not hay:
        return None
    # Storm / glacier / sands / peaks before Great Empire so «империя» leftovers
    # do not steal a unique other-world row.
    order = ("storm_islands", "everwinter", "burning_sands", "fire_peaks", "great_empire")
    for world_id in order:
        if blob_hits(hay, WORLD_BY_ID[world_id].ocr_needles):
            return world_id
    return None


def match_target_kind(text: str, kind: str) -> bool:
    spec = WORLD_BY_KIND.get(kind)
    if spec is None:
        return True
    hay = compact_ui(text)
    if not hay:
        return False
    if kind == "baron":
        return True
    hits = [token for token in spec.target_needles if token in hay]
    if kind == "barbarian_tower":
        return (
            "варварск" in hay
            or "варвар" in hay
            or "varvar" in hay
            or "barbarian" in hay
            or "башня" in hay
            or "bashn" in hay
            or "tower" in hay
        )
    if kind == "desert_tower":
        return "пустын" in hay or "pustyn" in hay or "desert" in hay
    if kind == "cultist_tower":
        return "культист" in hay or "kultist" in hay or "cultist" in hay
    if kind == "storm_fort":
        return "форт" in hay or "fort" in hay or "ураган" in hay
    return bool(hits)


_WRONG_WORLD_PLAQUE_NEEDLES = (
    "замок",
    "zamok",
    "3amok",
    "samak",
    "samok",
    "castle",
    "разбой",
    "razbo",
    "pasbo",
    "pasg",
    "pa3o",
    "pas6",
    "обзор",
    "obzor",
    "0630",
    "o630",
    "baron",
    "барон",
)


def looks_like_wrong_world_target(text: str) -> bool:
    """Robber/player castle or encyclopedia Обзор — not a world tower/fort."""
    hay = compact_ui(text)
    if not hay:
        return False
    return any(token in hay for token in _WRONG_WORLD_PLAQUE_NEEDLES)


def world_fill_decision(
    sent: int,
    quota: int,
    in_flight: int,
    *,
    first_must_skip: bool,
    wait_last: bool = True,
) -> str:
    """Latest policy: insufficient troops skip this world and continue the tour."""
    return "world_skip_empty"


def unopened_report_text(scan: NavigationScan | None, *, missing_ids: list[str] | None = None) -> str:
    missing = list(missing_ids if missing_ids is not None else (scan.missing_world_ids if scan else []))
    open_ids = list(scan.open_world_ids) if scan else []
    lines = ["Отчёт по мирам"]
    if open_ids:
        names = ", ".join(WORLD_BY_ID[item].display_ru for item in open_ids if item in WORLD_BY_ID)
        lines.append(f"открыты: {names}")
    if missing:
        names = [WORLD_BY_ID[item].display_ru if item in WORLD_BY_ID else item for item in missing]
        lines.append("не открыты: " + ", ".join(names))
    else:
        lines.append("все целевые миры открыты")
    lines.append("Storm Islands / Острова ураганов")
    if "storm_islands" in missing or (scan is None and "storm_islands" not in open_ids):
        lines.append(STORM_UNOPENED_LINE)
    elif "storm_islands" in open_ids:
        lines.append("острова ураганов открыты")
    if missing:
        lines.append(CONTINUE_ONE_WORLD_LINE)
    return "\n".join(lines)


def write_world_attack_report(
    scan: NavigationScan | None,
    *,
    extra_lines: list[str] | None = None,
) -> dict[str, Any]:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    text = unopened_report_text(scan)
    if extra_lines:
        text = text + "\n" + "\n".join(extra_lines)
    payload = {
        "text": text,
        "open_world_ids": list(scan.open_world_ids) if scan else [],
        "missing_world_ids": list(scan.missing_world_ids) if scan else [],
        "open_worlds": [
            WORLD_BY_ID[item].display_ru for item in (scan.open_world_ids if scan else []) if item in WORLD_BY_ID
        ],
        "missing_worlds": [
            WORLD_BY_ID[item].display_ru
            for item in (scan.missing_world_ids if scan else [])
            if item in WORLD_BY_ID
        ],
        "storm_islands_open": bool(scan and "storm_islands" in scan.open_world_ids),
        "continue_one_world": CONTINUE_ONE_WORLD_LINE,
    }
    json_path = DATA_DIR / "attack_report.json"
    txt_path = DATA_DIR / "attack_report.txt"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    txt_path.write_text(text, encoding="utf-8")
    return payload


def scan_from_blob(blob: str, rows: list[NavRow] | None = None) -> NavigationScan:
    hay = compact_ui(blob)
    open_ids: list[str] = []
    for spec in TOUR_WORLDS:
        if blob_hits(hay, spec.ocr_needles):
            open_ids.append(spec.id)
    if rows:
        for row in rows:
            if row.world_id and row.world_id not in open_ids and row.world_id != "great_empire":
                if WORLD_BY_ID.get(row.world_id) and WORLD_BY_ID[row.world_id].tour:
                    open_ids.append(row.world_id)
    missing = [spec.id for spec in TOUR_WORLDS if spec.id not in open_ids]
    return NavigationScan(
        rows=list(rows or []),
        open_world_ids=open_ids,
        missing_world_ids=missing,
        blob=hay,
    )


def spec_for_kind(kind: str) -> WorldSpec | None:
    if kind == "baron":
        return WORLD_BY_ID["great_empire"]
    return WORLD_BY_KIND.get(kind)


def is_world_npc_kind(kind: str) -> bool:
    return kind in WORLD_NPC_KINDS


def world_hunt_hits_are_cluster(
    points: list[tuple[float, float]],
    coords: list[tuple[int, int] | None] | None = None,
    *,
    map_span: int = 25,
    screen_span: float = 0.12,
) -> bool:
    """True when marks sit in one screen blob (one castle), not spread-out towers.

    Map-tile clustering is ignored: robber flags on one glacier castle often
    project to the same tile even when screen points are far apart.
    """
    del coords, map_span
    if len(points) < 2:
        return False
    xs = [item[0] for item in points]
    ys = [item[1] for item in points]
    return max(xs) - min(xs) <= screen_span and max(ys) - min(ys) <= screen_span


def world_payload() -> list[dict[str, Any]]:
    return [asdict(item) for item in WORLDS]
