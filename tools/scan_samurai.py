"""Scan the current map for samurai camps and daimyo castles. No clicks."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from e4kbot.bluestacks import capture_game_image  # noqa: E402
from e4kbot.client import load_layout  # noqa: E402
from e4kbot.config import load_config  # noqa: E402
from e4kbot.vision import (  # noqa: E402
    crop_rel,
    find_daimyo_candidates,
    find_samurai_candidates,
    ocr_text,
    parse_coordinate_pair,
    parse_count,
    project_map_coordinate,
)


def _read_coords(image, layout) -> tuple[tuple[int, int] | None, tuple[int, int] | None]:
    regions = layout.get("regions") or {}
    main_region = regions.get("main_castle_coords")
    x_region = regions.get("viewport_x")
    y_region = regions.get("viewport_y")
    main = (
        parse_coordinate_pair(ocr_text(crop_rel(image, main_region), psm=6))
        if main_region
        else None
    )
    vx = parse_count(ocr_text(crop_rel(image, x_region), psm=6)) if x_region else None
    vy = parse_count(ocr_text(crop_rel(image, y_region), psm=6)) if y_region else None
    viewport = (vx, vy) if vx is not None and vy is not None else None
    return main, viewport


def _project(point, viewport, vision) -> tuple[int, int] | None:
    if viewport is None:
        return None
    anchor_raw = vision.get("map_anchor") or [0.50, 0.54]
    scale_raw = vision.get("map_coordinate_scale") or [0.044, 0.044]
    coords = project_map_coordinate(
        point,
        viewport,
        (float(anchor_raw[0]), float(anchor_raw[1])),
        (float(scale_raw[0]), float(scale_raw[1])),
    )
    return (round(coords[0]), round(coords[1]))


def main() -> int:
    config = load_config()
    image = capture_game_image(config)
    if image is None:
        print("NO_SCREEN: BlueStacks window not found")
        return 2
    layout = load_layout(str((config.get("bluestacks") or {}).get("layout") or "default"))
    vision = config.get("vision") or {}
    main, viewport = _read_coords(image, layout)
    camps = find_samurai_candidates(image, max_hits=24)
    daimyo = find_daimyo_candidates(image, max_hits=24)
    camp_rows = []
    seen: set[tuple[int, int]] = set()
    for nx, ny, score in camps:
        coords = _project((nx, ny), viewport, vision)
        if coords and coords in seen:
            continue
        if coords:
            seen.add(coords)
        camp_rows.append(
            {
                "kind": "samurai_camp",
                "coords": list(coords) if coords else None,
                "screen": [round(nx, 3), round(ny, 3)],
                "score": round(score, 3),
            }
        )
    daimyo_rows = []
    for nx, ny, score in daimyo:
        coords = _project((nx, ny), viewport, vision)
        daimyo_rows.append(
            {
                "kind": "daimyo_castle",
                "coords": list(coords) if coords else None,
                "screen": [round(nx, 3), round(ny, 3)],
                "score": round(score, 3),
            }
        )
    payload = {
        "main_castle": list(main) if main else None,
        "viewport": list(viewport) if viewport else None,
        "samurai_camps": camp_rows,
        "samurai_count": len(camp_rows),
        "daimyo_castles": daimyo_rows,
        "daimyo_count": len(daimyo_rows),
        "note": "Current screen only. Coords estimated from map HUD; click plaque for exact XY.",
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
