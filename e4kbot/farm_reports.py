from __future__ import annotations

import hashlib
import io
import json
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import cv2
import numpy as np
from PIL import Image

from e4kbot.paths import DATA_DIR, ROOT
from e4kbot.vision import crop_rel, find_template_matches, ocr_text, ocr_text_ui

LEDGER_PATH = DATA_DIR / "farm_ledger.jsonl"
SUMMARY_PATH = DATA_DIR / "farm_summary.json"
PROGRESS_PATH = DATA_DIR / "overnight_progress.json"
ATTACK_VICTORY_ROW = ROOT / "assets" / "messages" / "attack_victory_row.png"

WORLD_BY_MODE = {
    "robber_barons": "Великая империя",
    "baron": "Великая империя",
    "barbarian_towers": "Вечнохолодный Ледник",
    "barbarian_tower": "Вечнохолодный Ледник",
    "desert_towers": "Пылающие Пески",
    "desert_tower": "Пылающие Пески",
    "cultist_towers": "Огненные Вершины",
    "cultist_tower": "Огненные Вершины",
    "storm_forts": "Острова ураганов",
    "storm_fort": "Острова ураганов",
}


def _number(image: Image.Image, region: list[float], *, signed: bool = False) -> int | None:
    raw = ocr_text(crop_rel(image, region), psm=7)
    match = re.search(r"-?\d[\d\s]*", raw)
    if not match:
        return None
    value = int(re.sub(r"[^\d]", "", match.group(0)))
    return -value if signed and "-" in match.group(0) else value


def _text(image: Image.Image, region: list[float], psm: int = 6) -> str:
    return " ".join(ocr_text_ui(crop_rel(image, region), psm=psm).split()).strip()


def image_identity(image: Image.Image) -> str:
    buffer = io.BytesIO()
    image.convert("RGB").save(buffer, format="PNG", optimize=False)
    return hashlib.sha256(buffer.getvalue()).hexdigest()


@dataclass
class FarmReport:
    id: str
    captured_at: float
    world: str = ""
    target: str = ""
    subject: str = ""
    report_timestamp: str = ""
    attacker: str = ""
    defender: str = ""
    own_soldiers: int | None = None
    own_losses: int | None = None
    defender_soldiers: int | None = None
    defender_losses: int | None = None
    resources: dict[str, int] = field(default_factory=dict)
    rewards: dict[str, int] = field(default_factory=dict)
    raw_ocr: str = ""
    screenshot: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def parse_victory_report(
    image: Image.Image,
    *,
    world: str = "",
    subject: str = "",
    report_timestamp: str = "",
    screenshot: str = "",
) -> FarmReport:
    """Parse only visible values. Missing/uncertain OCR remains absent, never fabricated."""
    title = _text(image, [0.16, 0.00, 0.84, 0.08], 7)
    attacker = _text(image, [0.08, 0.38, 0.44, 0.47], 6)
    defender = _text(image, [0.56, 0.38, 0.94, 0.47], 6)
    target = defender.splitlines()[0] if defender else ""
    resources: dict[str, int] = {}
    rewards: dict[str, int] = {}
    # The first four cells are stable in the E4K victory layout.
    for name, region in (
        ("wood", [0.22, 0.625, 0.36, 0.715]),
        ("food", [0.36, 0.625, 0.50, 0.715]),
        ("stone", [0.22, 0.705, 0.36, 0.795]),
        ("special", [0.36, 0.705, 0.50, 0.795]),
    ):
        value = _number(image, region)
        if value is not None:
            resources[name] = value
    for name, region in (
        ("coins", [0.13, 0.79, 0.25, 0.885]),
        ("other", [0.31, 0.79, 0.43, 0.885]),
        ("xp", [0.49, 0.79, 0.63, 0.885]),
        ("rubies", [0.67, 0.79, 0.79, 0.885]),
    ):
        value = _number(image, region, signed=name == "xp")
        if value is not None:
            rewards[name] = value
    raw = _text(image, [0.05, 0.00, 0.95, 0.90], 6)
    return FarmReport(
        id=image_identity(image),
        captured_at=time.time(),
        world=world,
        target=target,
        subject=subject or title,
        report_timestamp=report_timestamp,
        attacker=attacker,
        defender=defender,
        own_soldiers=_number(image, [0.23, 0.47, 0.36, 0.535]),
        own_losses=_number(image, [0.23, 0.515, 0.36, 0.58], signed=True),
        defender_soldiers=_number(image, [0.66, 0.47, 0.78, 0.535]),
        defender_losses=_number(image, [0.66, 0.515, 0.78, 0.58], signed=True),
        resources=resources,
        rewards=rewards,
        raw_ocr=raw,
        screenshot=screenshot,
    )


def is_victory_report(image: Image.Image) -> bool:
    blob = (_text(image, [0.08, 0.00, 0.92, 0.50], 6)).lower().replace("ё", "е")
    return "побед" in blob and ("напада" in blob or "солдат" in blob or "потер" in blob)


def _victory_row_templates() -> list[np.ndarray]:
    if not ATTACK_VICTORY_ROW.exists():
        return []
    full = cv2.imread(str(ATTACK_VICTORY_ROW))
    if full is None or full.size == 0:
        return []
    height = int(full.shape[0])
    top = full[: max(8, height // 2)]
    return [top, full]


def unread_battle_rows(image: Image.Image) -> list[tuple[float, float, str, str]]:
    """Find green unread rows and retain only battle/victory reports, never espionage."""
    rows: list[tuple[float, float, str, str]] = []
    for template in _victory_row_templates():
        hits = find_template_matches(
            image,
            template,
            threshold=0.58,
            x_min=0.04,
            x_max=0.96,
            y_min=0.20,
            y_max=0.94,
            max_hits=8,
            min_distance=36,
        )
        for _nx, ny, _score in hits:
            rows.append((0.50, float(ny), "Нападение: Победа", ""))
    if rows:
        rows.sort(key=lambda item: item[1])
        unique: list[tuple[float, float, str, str]] = []
        for row in rows:
            if any(abs(row[1] - other[1]) < 0.04 for other in unique):
                continue
            unique.append(row)
        return unique
    rgb = np.asarray(image.convert("RGB"))
    hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)
    height, width = hsv.shape[:2]
    roi = hsv[int(0.28 * height) : int(0.90 * height), int(0.03 * width) : int(0.97 * width)]
    green = cv2.inRange(roi, (25, 25, 85), (95, 255, 255))
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (max(5, width // 18), 5))
    joined = cv2.morphologyEx(green, cv2.MORPH_CLOSE, kernel)
    count, _, stats, centers = cv2.connectedComponentsWithStats(joined)
    rows: list[tuple[float, float, str, str]] = []
    for index in range(1, count):
        x, y, w, h, area = (int(v) for v in stats[index])
        if area < width * height * 0.012 or w < width * 0.45 or h < height * 0.035:
            continue
        cy = (float(centers[index][1]) + 0.28 * height) / height
        if not 0.30 <= cy <= 0.90:
            continue
        y1 = max(0.22, cy - max(0.045, h / height))
        y2 = min(0.94, cy + max(0.045, h / height))
        text = _text(image, [0.05, y1, 0.94, y2], 6)
        lowered = text.lower().replace("ё", "е")
        if "шпион" in lowered or "spy" in lowered:
            continue
        if not any(token in lowered for token in ("напад", "побед", "замок разбой", "battle", "victory")):
            continue
        parts = [part.strip() for part in re.split(r"\s{2,}|\n", text) if part.strip()]
        subject = parts[0] if parts else text
        stamp = parts[-1] if len(parts) > 1 else ""
        rows.append((0.50, cy, subject, stamp))
    rows.sort(key=lambda item: item[1])
    return rows


def find_messages_button(image: Image.Image) -> tuple[float, float] | None:
    """Locate «Сообщения» only in the lower navigation strip."""
    try:
        import pytesseract

        bottom = crop_rel(image, [0.00, 0.72, 1.00, 1.00])
        data = pytesseract.image_to_data(
            bottom.resize((bottom.width * 3, bottom.height * 3)),
            config="--psm 11 -l rus+eng",
            output_type=pytesseract.Output.DICT,
            timeout=10,
        )
        for index, raw in enumerate(data.get("text") or []):
            text = str(raw or "").lower().replace("ё", "е")
            if not any(token in text for token in ("сообщ", "message")):
                continue
            x = (float(data["left"][index]) + float(data["width"][index]) / 2) / (bottom.width * 3)
            y = (float(data["top"][index]) + float(data["height"][index]) / 2) / (bottom.height * 3)
            # Tap the envelope above its caption, not a neighbouring nav button.
            return min(0.96, max(0.04, x)), min(0.96, max(0.73, 0.72 + y * 0.28 - 0.055))
    except Exception:
        return None
    return None


class FarmLedger:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or LEDGER_PATH

    def rows(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        result: list[dict[str, Any]] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except (ValueError, TypeError):
                continue
            if isinstance(row, dict):
                result.append(row)
        return result

    def append(self, report: FarmReport) -> bool:
        rows = self.rows()
        if any(str(row.get("id")) == report.id for row in rows):
            return False
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(report.to_dict(), ensure_ascii=False) + "\n")
        self.write_summary()
        return True

    def summary(self) -> dict[str, Any]:
        rows = self.rows()
        totals: dict[str, Any] = {
            "reports": len(rows),
            "own_soldiers": 0,
            "own_losses": 0,
            "defender_soldiers": 0,
            "defender_losses": 0,
            "resources": {},
            "rewards": {},
            "by_world": {},
            "updated_at": time.time(),
        }
        for row in rows:
            for key in ("own_soldiers", "own_losses", "defender_soldiers", "defender_losses"):
                value = row.get(key)
                if isinstance(value, int):
                    totals[key] += value
            for bucket in ("resources", "rewards"):
                for name, value in (row.get(bucket) or {}).items():
                    if isinstance(value, int):
                        totals[bucket][name] = int(totals[bucket].get(name) or 0) + value
            world = str(row.get("world") or "Не определён")
            world_row = totals["by_world"].setdefault(world, {"reports": 0, "own_losses": 0, "resources": {}})
            world_row["reports"] += 1
            if isinstance(row.get("own_losses"), int):
                world_row["own_losses"] += int(row["own_losses"])
            for name, value in (row.get("resources") or {}).items():
                if isinstance(value, int):
                    world_row["resources"][name] = int(world_row["resources"].get(name) or 0) + value
        return totals

    def write_summary(self) -> dict[str, Any]:
        payload = self.summary()
        SUMMARY_PATH.parent.mkdir(parents=True, exist_ok=True)
        SUMMARY_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return payload


def write_progress(**updates: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    if PROGRESS_PATH.exists():
        try:
            payload = json.loads(PROGRESS_PATH.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            payload = {}
    payload.update(updates)
    payload["updated_at"] = time.time()
    PROGRESS_PATH.parent.mkdir(parents=True, exist_ok=True)
    PROGRESS_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload
