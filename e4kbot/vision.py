from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import cv2
import numpy as np
from PIL import Image

from e4kbot.paths import ROOT

REFERENCE_SIZE = (900, 1600)
# Right-edge special-offers rail (shop/crown/bag, ruby chest, timed chest).
OFFER_RAIL_X = 0.82
SPECIAL_OFFERS_CLOSE_FALLBACK = (0.93, 0.04)
ROBBER_TEMPLATE = ROOT / "assets" / "robber_castle.png"
SAMURAI_TEMPLATE = ROOT / "assets" / "samurai_camp.png"
PICKER_MAX_TEMPLATE = ROOT / "assets" / "picker_max.png"
TARGET_ATTACK_TEMPLATE = ROOT / "assets" / "target_attack.png"
PICKER_CONFIRM_TEMPLATE = ROOT / "assets" / "picker_confirm.png"
NO_COMMANDERS_TEMPLATE = ROOT / "assets" / "no_commanders.png"
TOOL_BONUS_TEMPLATE = ROOT / "assets" / "tool_bonus_plus.png"
PRESET_BUTTON_TEMPLATE = ROOT / "assets" / "preset_button.png"
APPLY_PRESET_ALL_TEMPLATE = ROOT / "assets" / "apply_preset_all.png"
PRESETS_DIALOG_TEMPLATE = ROOT / "assets" / "presets_dialog.png"
AUTOSELECT_BUTTON_TEMPLATE = ROOT / "assets" / "autoselect_waves.png"
AUTOSELECT_DIALOG_TEMPLATE = ROOT / "assets" / "autoselect_dialog.png"
SPECIAL_OFFERS_MARKERS = (
    "спецпредлож",
    "спецпредл",
    "specialoffer",
    "special offer",
    "новое предлож",
    "новое предложение",
    "набор новичка",
    "hoboe",
    "npeas",
    "cneunp",
    "npeanox",
    "спецпредложение",
    "прайм-тайм",
    "прайм тайм",
    "npamam-tamm",
    "npamamtamm",
    "mipavm-tamm",
    "mipavmtamm",
)


def crop_rel(image: Image.Image, region: list[float]) -> Image.Image:
    width, height = image.size
    x1, y1, x2, y2 = region
    return image.crop(
        (
            round(x1 * width),
            round(y1 * height),
            round(x2 * width),
            round(y2 * height),
        )
    )


def ocr_text(image: Image.Image, psm: int = 7) -> str:
    return _ocr_raw(image, psm, "0123456789:/")


def ocr_text_ui(image: Image.Image, psm: int = 6) -> str:
    return _ocr_raw(image, psm, None)


def _tessdata_dir() -> Path | None:
    local = ROOT / "tessdata"
    if (local / "eng.traineddata").exists():
        return local
    system = Path(r"C:\Program Files\Tesseract-OCR\tessdata")
    if (system / "eng.traineddata").exists():
        return system
    return None


def _ocr_raw(image: Image.Image, psm: int, whitelist: str | None) -> str:
    try:
        import pytesseract

        default_binary = Path(r"C:\Program Files\Tesseract-OCR\tesseract.exe")
        if default_binary.exists():
            pytesseract.pytesseract.tesseract_cmd = str(default_binary)
        gray = cv2.cvtColor(np.asarray(image.convert("RGB")), cv2.COLOR_RGB2GRAY)
        gray = cv2.resize(gray, None, fx=2.5, fy=2.5, interpolation=cv2.INTER_CUBIC)
        gray = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1]
        tessdata = _tessdata_dir()
        config = f"--psm {psm}"
        if whitelist:
            config += f" -c tessedit_char_whitelist={whitelist}"
        elif tessdata is not None and (tessdata / "rus.traineddata").exists():
            config += f' --tessdata-dir "{tessdata.as_posix()}" -l rus+eng'
        else:
            config += " -l rus+eng"
        return pytesseract.image_to_string(gray, config=config).strip()
    except Exception:
        return ""


def parse_percent(text: str) -> int | None:
    match = re.search(r"\+?\s*(\d{1,3})\s*%", text.replace(" ", ""))
    if not match:
        return None
    return int(match.group(1))


def parse_bonus_percent(text: str) -> int | None:
    """Combat tool bonus is always written as +N%. Ignore wall -25% and ruby prices."""
    match = re.search(r"\+(\d{1,2})\s*%", (text or "").replace(" ", ""))
    if not match:
        return None
    value = int(match.group(1))
    return value if 1 <= value <= 40 else None


def is_offer_rail_point(nx: float, ny: float | None = None) -> bool:
    """True for the vertical special-offers rail, not the bottom action bar."""
    if nx < OFFER_RAIL_X:
        return False
    if ny is not None and ny >= 0.90:
        return False
    return True


def _normalize_ui_text(text: str) -> str:
    return text.lower().replace("ё", "е").replace("-", "").replace(" ", "")


def is_special_offers_screen(
    image: Image.Image,
    recognized_text: str | None = None,
) -> bool:
    """True only when the «спецпредложения» title is present — never formation/map chrome."""
    if recognized_text is None:
        top = ocr_text_ui(crop_rel(image, [0.05, 0.0, 0.95, 0.28]), psm=6)
        mid = ocr_text_ui(crop_rel(image, [0.12, 0.28, 0.88, 0.62]), psm=6)
        recognized_text = f"{top} {mid}"
    text = recognized_text.lower().replace("ё", "е")
    compact = _normalize_ui_text(recognized_text)
    if any(
        marker in text or _normalize_ui_text(marker) in compact
        for marker in SPECIAL_OFFERS_MARKERS
    ):
        return True
    return _looks_like_special_offers_overlay(image)


def _looks_like_special_offers_overlay(image: Image.Image) -> bool:
    """Legacy layout hint; must not be used alone — formation has the same title-bar X."""
    bgr = _reference_bgr(image)
    hsv = cv2.cvtColor(bgr[180:1180, 70:800], cv2.COLOR_BGR2HSV)
    green = cv2.inRange(hsv, (30, 45, 35), (95, 255, 255))
    if float(np.count_nonzero(green)) / max(1, green.size) > 0.16:
        return False
    red = find_red_cross_force(
        image,
        allow_right_chrome=True,
        title_bar_only=True,
    )
    if red is None:
        return False
    return red[0] >= 0.88 and red[1] <= 0.16


TAXES_MARKERS = (
    "налог",
    "hanor",
    "hanorn",
    "taxes",
    "оставшееся время",
    "octasp",
)


def is_taxes_dialog(
    image: Image.Image,
    recognized_text: str | None = None,
) -> bool:
    """Parchment «Налоги» over the map. Never the bribe/+20% control."""
    if recognized_text is None:
        title = crop_rel(image, [0.05, 0.00, 0.95, 0.28])
        recognized_text = ocr_text_ui(title, psm=6)
    text = recognized_text.lower().replace("ё", "е")
    compact = _normalize_ui_text(recognized_text)
    return any(
        marker in text or _normalize_ui_text(marker) in compact
        for marker in TAXES_MARKERS
    )


def find_parchment_title_close(image: Image.Image) -> tuple[float, float] | None:
    """White X on a parchment title bar. Never shop rail, never a red +% badge."""
    bgr = _reference_bgr(image)
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    y1 = int(0.01 * REFERENCE_SIZE[1])
    y2 = int(0.12 * REFERENCE_SIZE[1])
    x1 = int(0.70 * REFERENCE_SIZE[0])
    x2 = int(0.90 * REFERENCE_SIZE[0])
    white = cv2.inRange(hsv[y1:y2, x1:x2], (0, 0, 180), (180, 50, 255))
    count, _, stats, centers = cv2.connectedComponentsWithStats(white)
    best: tuple[int, float, float] | None = None
    for index in range(1, count):
        _x, _y, width, height, area = stats[index]
        if not (400 <= area <= 2500 and 20 <= width <= 70 and 20 <= height <= 70):
            continue
        ratio = width / max(1, height)
        if not (0.7 <= ratio <= 1.4):
            continue
        nx = (centers[index][0] + x1) / REFERENCE_SIZE[0]
        ny = (centers[index][1] + y1) / REFERENCE_SIZE[1]
        if nx >= 0.88 or nx < 0.78 or ny > 0.09:
            continue
        if best is None or area > best[0]:
            best = (int(area), nx, ny)
    if best:
        return (best[1], best[2])
    return None


def find_mid_offer_close(image: Image.Image) -> tuple[float, float] | None:
    """Red X on the in-map «don't show again» row. Never shop / view-offer buttons."""
    bgr = _reference_bgr(image)
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    y1 = int(0.50 * REFERENCE_SIZE[1])
    y2 = int(0.62 * REFERENCE_SIZE[1])
    x1 = int(0.72 * REFERENCE_SIZE[0])
    x2 = int(0.88 * REFERENCE_SIZE[0])
    red = cv2.bitwise_or(
        cv2.inRange(hsv[y1:y2, x1:x2], (0, 110, 70), (14, 255, 255)),
        cv2.inRange(hsv[y1:y2, x1:x2], (165, 110, 70), (180, 255, 255)),
    )
    count, _, stats, centers = cv2.connectedComponentsWithStats(red)
    best: tuple[float, int, float, float] | None = None
    for index in range(1, count):
        _x, _y, width, height, area = stats[index]
        if not (250 <= area <= 4500):
            continue
        ratio = width / max(1, height)
        if not (0.55 <= ratio <= 3.5):
            continue
        nx = (centers[index][0] + x1) / REFERENCE_SIZE[0]
        ny = (centers[index][1] + y1) / REFERENCE_SIZE[1]
        if ny >= 0.62 or nx >= 0.88 or nx < 0.74:
            continue
        square = abs(1.0 - ratio)
        if best is None or square < best[0] or (square == best[0] and area < best[1]):
            best = (square, int(area), nx, ny)
    if best:
        return (best[2], best[3])
    return (0.841, 0.577)


def special_offers_close_point(image: Image.Image) -> tuple[float, float]:
    """Close X of the special-offers overlay only — never buy / chest / rail / ruby HUD."""
    red = find_red_cross_force(
        image,
        allow_right_chrome=True,
        title_bar_only=True,
    )
    if red is not None and red[0] >= 0.88 and red[1] <= 0.18:
        return red
    mid = find_mid_offer_close(image)
    if mid is not None:
        return mid
    return SPECIAL_OFFERS_CLOSE_FALLBACK


def find_template_point(
    image: Image.Image,
    template_path: Path,
    threshold: float = 0.62,
) -> tuple[float, float, float] | None:
    if not template_path.exists():
        return None
    bgr = _reference_bgr(image)
    template = cv2.imread(str(template_path))
    if template is None or template.size == 0:
        return None
    if template.shape[0] >= bgr.shape[0] or template.shape[1] >= bgr.shape[1]:
        scale = min(
            (bgr.shape[1] * 0.92) / template.shape[1],
            (bgr.shape[0] * 0.92) / template.shape[0],
            1.0,
        )
        template = cv2.resize(
            template,
            (
                max(8, int(template.shape[1] * scale)),
                max(8, int(template.shape[0] * scale)),
            ),
            interpolation=cv2.INTER_AREA,
        )
    result = cv2.matchTemplate(bgr, template, cv2.TM_CCOEFF_NORMED)
    _, score, _, location = cv2.minMaxLoc(result)
    if score < threshold:
        return None
    x = location[0] + template.shape[1] / 2
    y = location[1] + template.shape[0] / 2
    return x / REFERENCE_SIZE[0], y / REFERENCE_SIZE[1], float(score)


def _find_scaled_navigation_template(
    image: Image.Image,
    template_path: Path,
    threshold: float,
) -> tuple[float, float, float, float] | None:
    """Match a crop-derived menu template at the current game UI scale."""
    if not template_path.exists():
        return None
    bgr = cv2.cvtColor(np.asarray(image.convert("RGB")), cv2.COLOR_RGB2BGR)
    template = cv2.imread(str(template_path))
    if template is None or template.size == 0:
        return None
    expected = max(0.45, image.width / 506.0)
    scales = sorted(
        {
            round(expected * factor, 3)
            for factor in (0.82, 0.90, 0.96, 1.0, 1.04, 1.10, 1.18)
        }
        | {1.0}
    )
    best: tuple[float, float, float, float] | None = None
    for scale in scales:
        width = max(8, round(template.shape[1] * scale))
        height = max(8, round(template.shape[0] * scale))
        if width >= image.width or height >= image.height:
            continue
        interpolation = cv2.INTER_CUBIC if scale > 1 else cv2.INTER_AREA
        resized = cv2.resize(template, (width, height), interpolation=interpolation)
        scores = cv2.matchTemplate(bgr, resized, cv2.TM_CCOEFF_NORMED)
        _, score, _, location = cv2.minMaxLoc(scores)
        candidate = (
            (location[0] + width / 2) / image.width,
            (location[1] + height / 2) / image.height,
            float(score),
            float(scale),
        )
        if best is None or candidate[2] > best[2]:
            best = candidate
    return best if best is not None and best[2] >= threshold else None


def _validated_navigation_panel(
    image: Image.Image,
    point: tuple[float, float],
    scale: float,
) -> tuple[float, float, float, float] | None:
    """Require the wide beige navigation strip around a matched menu control."""
    cx, cy = point[0] * image.width, point[1] * image.height
    panel_width = min(float(image.width), 506.0 * scale)
    panel_height = min(float(image.height), 146.0 * scale)
    left = max(0, round(cx - 0.94 * panel_width))
    right = min(image.width, round(cx + 0.06 * panel_width))
    top = max(0, round(cy - 0.58 * panel_height))
    bottom = min(image.height, round(cy + 0.42 * panel_height))
    if right - left < 0.68 * image.width or bottom - top < 35:
        return None
    patch = cv2.cvtColor(
        np.asarray(image.convert("RGB"))[top:bottom, left:right],
        cv2.COLOR_RGB2HSV,
    )
    if patch.size == 0:
        return None
    beige = cv2.inRange(patch, (5, 10, 105), (38, 205, 255))
    beige_ratio = float(np.count_nonzero(beige)) / beige.size
    if beige_ratio < 0.42:
        return None
    return (
        left / image.width,
        top / image.height,
        right / image.width,
        bottom / image.height,
    )


def parse_ratio(text: str) -> tuple[int, int] | None:
    match = re.search(r"(\d+)\s*/\s*(\d+)", text)
    if not match:
        return None
    current_text, capacity_text = match.group(1), match.group(2)
    current, capacity = int(current_text), int(capacity_text)
    if current > capacity:
        # OCR can prepend the small unit-slot count/icon to the real value
        # (for example "1 05/7" -> "105/7"). Recover the longest valid suffix.
        for index in range(1, len(current_text)):
            candidate = int(current_text[index:])
            if candidate <= capacity:
                current = candidate
                break
    return current, capacity


def parse_count(text: str) -> int | None:
    match = re.search(r"\d+", text.replace("O", "0"))
    return int(match.group()) if match else None


def parse_coordinate_pair(text: str) -> tuple[int, int] | None:
    values = [int(value) for value in re.findall(r"\d+", text)]
    return (values[-2], values[-1]) if len(values) >= 2 else None


def project_map_coordinate(
    point: tuple[float, float],
    viewport: tuple[int, int],
    anchor: tuple[float, float] = (0.50, 0.54),
    coordinate_scale: tuple[float, float] = (0.044, 0.044),
) -> tuple[float, float]:
    sx, sy = coordinate_scale
    return (
        viewport[0] + (point[0] - anchor[0]) / sx,
        viewport[1] + (point[1] - anchor[1]) / sy,
    )


def choose_nearest_main_castle(
    candidates: list[tuple[float, float, float]],
    main_castle: tuple[int, int],
    viewport: tuple[int, int],
    anchor: tuple[float, float] = (0.50, 0.54),
    coordinate_scale: tuple[float, float] = (0.044, 0.044),
) -> tuple[float, float] | None:
    if not candidates:
        return None
    ranked: list[tuple[float, tuple[float, float]]] = []
    for nx, ny, _ in candidates:
        map_x, map_y = project_map_coordinate(
            (nx, ny), viewport, anchor, coordinate_scale
        )
        distance = (map_x - main_castle[0]) ** 2 + (map_y - main_castle[1]) ** 2
        ranked.append((distance, (nx, ny)))
    return min(ranked, key=lambda item: item[0])[1]


def _reference_bgr(image: Image.Image) -> np.ndarray:
    rgb = np.asarray(image.convert("RGB"))
    bgr = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
    return cv2.resize(bgr, REFERENCE_SIZE, interpolation=cv2.INTER_AREA)


def is_map_screen(image: Image.Image) -> bool:
    bgr = _reference_bgr(image)
    hsv = cv2.cvtColor(bgr[140:1420, 0:900], cv2.COLOR_BGR2HSV)
    green = cv2.inRange(hsv, (30, 45, 35), (95, 255, 255))
    return float(np.count_nonzero(green)) / green.size > 0.28


def is_inbox_screen(image: Image.Image) -> bool:
    """Mail / «Входящие» overlay, including «Удалить все» confirm. Not the kingdom map."""
    top = _ocr_compact(ocr_text_ui(crop_rel(image, [0.12, 0.00, 0.88, 0.16]), psm=6))
    mid = _ocr_compact(ocr_text_ui(crop_rel(image, [0.16, 0.28, 0.84, 0.62]), psm=6))
    blob = f"{top}{mid}"
    markers = (
        "входящ",
        "vkhodyash",
        "всесообщен",
        "удалитьвсе",
        "хочешьудалить",
        "игнорируем",
        "ignoriruem",
        "inbox",
    )
    return any(token in blob for token in markers)


def is_formation_screen(image: Image.Image) -> bool:
    """Attack-planning screen: parchment plus a wave header (often above y=0.61)."""
    if _no_commanders_unique_template_score(image) >= 0.48:
        return False
    if is_special_offers_screen(image):
        return False
    wave = formation_wave_diagnostics(image)
    bgr = _reference_bgr(image)
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    brown = cv2.inRange(hsv[0:940], (3, 45, 20), (30, 255, 180))
    brown_ratio = float(np.count_nonzero(brown)) / brown.size
    if wave.get("first_header") and brown_ratio > 0.12:
        return True
    yellow = cv2.inRange(hsv[980:1210], (15, 80, 105), (45, 255, 255))
    full_width_band_rows = int(np.count_nonzero(np.mean(yellow > 0, axis=1) > 0.75))
    return brown_ratio > 0.18 and full_width_band_rows >= 25


def is_travel_dialog(image: Image.Image) -> bool:
    if _no_commanders_unique_template_score(image) >= 0.48:
        return False
    bgr = _reference_bgr(image)
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    brown = cv2.inRange(hsv[230:1360, 70:830], (3, 35, 20), (30, 255, 190))
    green = cv2.inRange(hsv[1240:1380, 500:840], (35, 80, 50), (95, 255, 255))
    return (
        float(np.count_nonzero(brown)) / brown.size > 0.32
        and float(np.count_nonzero(green)) / green.size > 0.10
    )


def find_formation_attack_button(image: Image.Image) -> tuple[float, float] | None:
    """Gold «Нападение» on the planning footer. Never the title-bar close."""
    bgr = _reference_bgr(image)
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    top = int(0.90 * REFERENCE_SIZE[1])
    left = int(0.52 * REFERENCE_SIZE[0])
    band = hsv[top : REFERENCE_SIZE[1], left : int(0.98 * REFERENCE_SIZE[0])]
    gold = cv2.inRange(band, (14, 70, 90), (48, 255, 255))
    count, _, stats, centers = cv2.connectedComponentsWithStats(gold)
    best: tuple[int, float, float] | None = None
    for index in range(1, count):
        _x, _y, width, height, area = stats[index]
        if area < 400:
            continue
        ratio = width / max(1, height)
        if not (1.4 < ratio < 8.0):
            continue
        cx, cy = centers[index]
        nx = (float(cx) + left) / REFERENCE_SIZE[0]
        ny = (float(cy) + top) / REFERENCE_SIZE[1]
        if ny < 0.90 or nx < 0.55 or nx > 0.98:
            continue
        candidate = (int(area), nx, ny)
        if best is None or candidate[0] > best[0]:
            best = candidate
    if best is None:
        return None
    return best[1], best[2]


def popup_action(
    image: Image.Image,
    recognized_text: str | None = None,
) -> tuple[float, float] | None:
    """Return a red-X close point only. Never buy/green and never map chrome/ruby HUD."""
    if is_special_offers_screen(image, recognized_text):
        return special_offers_close_point(image)
    if is_map_screen(image) or is_formation_screen(image) or is_travel_dialog(image):
        return None
    bgr = _reference_bgr(image)
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    red = cv2.bitwise_or(
        cv2.inRange(hsv, (0, 120, 90), (12, 255, 255)),
        cv2.inRange(hsv, (168, 120, 90), (180, 255, 255)),
    )
    count, _, stats, centers = cv2.connectedComponentsWithStats(red)
    close_candidates: list[tuple[int, float, float]] = []
    for index in range(1, count):
        x, y, width, height, area = stats[index]
        if area < 500 or not (0.55 < width / max(1, height) < 1.6):
            continue
        cx, cy = centers[index]
        nx = cx / REFERENCE_SIZE[0]
        ny = cy / REFERENCE_SIZE[1]
        if is_offer_rail_point(nx, ny):
            continue
        if cx > 0.70 * REFERENCE_SIZE[0] and cy < 0.38 * REFERENCE_SIZE[1]:
            close_candidates.append((area, cx, cy))
    if close_candidates:
        _, x, y = max(close_candidates)
        return x / REFERENCE_SIZE[0], y / REFERENCE_SIZE[1]
    return None


def find_red_cross_force(
    image: Image.Image,
    *,
    allow_right_chrome: bool = False,
    title_bar_only: bool = False,
) -> tuple[float, float] | None:
    """Aggressively find a red close/X control; never the green confirm."""
    bgr = _reference_bgr(image)
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    red = cv2.bitwise_or(
        cv2.inRange(hsv, (0, 110, 70), (14, 255, 255)),
        cv2.inRange(hsv, (165, 110, 70), (180, 255, 255)),
    )
    count, _, stats, centers = cv2.connectedComponentsWithStats(red)
    ranked: list[tuple[float, float, float]] = []
    for index in range(1, count):
        x, y, width, height, area = stats[index]
        cx, cy = centers[index]
        if area < 280:
            continue
        ratio = width / max(1, height)
        square = 0.45 < ratio < 1.9
        if not square:
            continue
        if title_bar_only and cy > 0.22 * REFERENCE_SIZE[1]:
            continue
        if not allow_right_chrome and cx >= OFFER_RAIL_X * REFERENCE_SIZE[0]:
            continue
        # Resource-bar / plus-ruby HUD, not a modal close X.
        if cy < 0.10 * REFERENCE_SIZE[1] and cx < 0.88 * REFERENCE_SIZE[0]:
            continue
        # Prefer parchment-top-right ribbon, then large lower-left decline seal.
        top_right = cx > 0.55 * REFERENCE_SIZE[0] and cy < 0.48 * REFERENCE_SIZE[1]
        lower_left_seal = (
            not title_bar_only
            and cx < 0.50 * REFERENCE_SIZE[0]
            and cy > 0.52 * REFERENCE_SIZE[1]
            and area >= 1200
        )
        if not (top_right or lower_left_seal):
            continue
        score = float(area)
        if top_right:
            score += 8000
        if title_bar_only:
            score += max(0.0, (0.22 * REFERENCE_SIZE[1] - cy) * 20)
        ranked.append((score, float(cx), float(cy)))
    if not ranked:
        return None
    _, x, y = max(ranked)
    return x / REFERENCE_SIZE[0], y / REFERENCE_SIZE[1]


def is_green_hire_point(nx: float, ny: float) -> bool:
    """True for the bottom-right green hire/check seal — never click it."""
    return nx > 0.52 and ny > 0.55


def _ocr_compact(text: str) -> str:
    """Letters/digits only so Latin-garbled Tesseract still matches Russian UI."""
    return "".join(ch for ch in (text or "").lower().replace("ё", "е") if ch.isalnum())


_COMMANDER_TOKENS = (
    "военачаль",
    "наместник",
    "командир",
    "boehay",
    "bochan",
    "bochay",
    "boeeha",
    "voenach",
    "namestn",
)
_NONE_FREE_TOKENS = (
    "нетсвобод",
    "hetcbogo",
    "hetcbooo",
    "cbogoqh",
    "cboooqh",
    "свободных",
)
_HIRE_RESERVE_TOKENS = (
    "нанятьрезерв",
    "hanatbpe",
    "резервн",
    "pe3epb",
    "pezepb",
    "pezerb",
    "pesepb",
)


def _no_commanders_text_hit(text: str) -> bool:
    """True for «нет свободных военачальников/наместников» hire parchment.

    Tesseract often emits Latin lookalikes: HeT CBOGOQHbIX BOCHANANbHMKOB.
    """
    blob = _ocr_compact(text)
    if not blob:
        return False
    if "начатьнападен" in blob or "nachatnapaden" in blob:
        return False
    commander = any(token in blob for token in _COMMANDER_TOKENS)
    none_free = any(token in blob for token in _NONE_FREE_TOKENS)
    hire_reserve = any(token in blob for token in _HIRE_RESERVE_TOKENS)
    return commander and (none_free or hire_reserve)


def _inscription_ruby_price(text: str) -> int | None:
    """Price from «Цена: 125» / garbled «Uena: 125» / «LleHa: 125»."""
    blob = _ocr_compact(text)
    labeled = re.search(r"(?:cena|uena|lleha|цена|ueha)(\d{2,4})", blob)
    if labeled:
        value = int(labeled.group(1))
        if 50 <= value <= 5000:
            return value
    for raw in re.findall(r"\d{2,4}", text or ""):
        value = int(raw)
        if 50 <= value <= 5000:
            return value
    return None


def _no_commanders_conclusion(text: str) -> str:
    """Turn the parchment inscription into an action, not a canned label."""
    blob = _ocr_compact(text)
    parts: list[str] = []
    namestnik = "наместник" in blob or "namestn" in blob
    voenachal = any(
        token in blob
        for token in ("военачаль", "boehay", "bochan", "bochay", "boeeha", "voenach")
    )
    if any(token in blob for token in _NONE_FREE_TOKENS):
        if namestnik and not voenachal:
            parts.append("надпись: нет свободных наместников")
        elif voenachal and not namestnik:
            parts.append("надпись: нет свободных военачальников")
        else:
            parts.append("надпись: нет свободных военачальников/наместников")
    elif any(token in blob for token in _COMMANDER_TOKENS):
        parts.append("надпись: нет свободных военачальников/наместников")
    if any(token in blob for token in _HIRE_RESERVE_TOKENS):
        parts.append("предлагают нанять резерв")
    price = _inscription_ruby_price(text)
    if price is not None:
        parts.append(f"цена {price} рубинов")
    elif "рубин" in blob or "rubin" in blob:
        parts.append("цена в рубинах")
    if not parts:
        parts.append("табличка найма военачальника")
    parts.append("нанимать нельзя — закрываю красным крестиком и жду возврат")
    return "; ".join(parts)


def _no_commanders_unique_template_score(image: Image.Image) -> float:
    """Match the 125-ruby price strip — the part that is not shared with travel confirm."""
    if not NO_COMMANDERS_TEMPLATE.exists():
        return 0.0
    template = cv2.imread(str(NO_COMMANDERS_TEMPLATE))
    if template is None or template.size == 0:
        return 0.0
    th, tw = template.shape[:2]
    price = template[int(th * 0.48) : int(th * 0.72), int(tw * 0.30) : int(tw * 0.70)]
    if price.size == 0:
        return 0.0
    bgr = cv2.cvtColor(np.asarray(image.convert("RGB")), cv2.COLOR_RGB2BGR)
    ih, iw = bgr.shape[:2]
    ph, pw = price.shape[:2]
    best = 0.0
    if ph <= ih and pw <= iw:
        best = max(
            best,
            float(cv2.minMaxLoc(cv2.matchTemplate(bgr, price, cv2.TM_CCOEFF_NORMED))[1]),
        )
    for scale in (0.7, 0.9, 1.15, 1.4, 1.7, 2.0, 2.4):
        width = max(16, int(pw * scale))
        height = max(12, int(ph * scale))
        if width >= iw or height >= ih:
            continue
        resized = cv2.resize(price, (width, height), interpolation=cv2.INTER_AREA)
        score = float(cv2.minMaxLoc(cv2.matchTemplate(bgr, resized, cv2.TM_CCOEFF_NORMED))[1])
        if score > best:
            best = score
    return best


def _no_commanders_red_closes(image: Image.Image) -> list[tuple[float, float, float]]:
    """Native-resolution red X candidates: score, nx, ny. Never the green hire seal."""
    rgb = np.asarray(image.convert("RGB"))
    height, width = rgb.shape[:2]
    hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)
    red = cv2.bitwise_or(
        cv2.inRange(hsv, (0, 80, 60), (14, 255, 255)),
        cv2.inRange(hsv, (165, 80, 60), (180, 255, 255)),
    )
    min_area = max(80, int(width * height * 0.0004))
    count, _, stats, centers = cv2.connectedComponentsWithStats(red)
    ranked: list[tuple[float, float, float]] = []
    for index in range(1, count):
        _x, _y, bw, bh, area = stats[index]
        cx, cy = centers[index]
        nx, ny = float(cx / width), float(cy / height)
        if area < min_area:
            continue
        ratio = bw / max(1, bh)
        if not (0.45 < ratio < 2.4):
            continue
        if is_green_hire_point(nx, ny):
            continue
        if ny < 0.10 and nx < 0.88:
            continue
        top_right = nx > 0.55 and ny < 0.48
        lower_left = nx < 0.50 and ny > 0.52
        if not (top_right or lower_left):
            continue
        score = float(area)
        if top_right:
            score += 8000
        ranked.append((score, nx, ny))
    ranked.sort(reverse=True)
    return ranked


def _looks_like_hire_parchment(image: Image.Image) -> bool:
    """Beige panel plus a red close. Shared chrome with travel — never sufficient alone."""
    rgb = np.asarray(image.convert("RGB"))
    hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)
    beige = cv2.inRange(hsv, (8, 12, 90), (42, 190, 255))
    beige_ratio = float(np.count_nonzero(beige)) / max(1, beige.size)
    if beige_ratio < 0.10:
        return False
    return bool(_no_commanders_red_closes(image))


def _parchment_ocr(image: Image.Image) -> str:
    """OCR the hire/attention parchment; full image if it is already a crop."""
    width, height = image.size
    if width <= 600 or height <= 500:
        return ocr_text_ui(image, psm=6)
    return ocr_text_ui(crop_rel(image, [0.08, 0.18, 0.92, 0.70]), psm=6)


def no_commanders_diagnostics(
    image: Image.Image,
    recognized_text: str | None = None,
) -> dict[str, Any]:
    """Hire-reserve parchment only. Never the green 125-ruby seal."""
    diagnostic: dict[str, Any] = {
        "point": None,
        "popup_bounds": None,
        "red_ratio": 0.0,
        "text": "",
        "valid": False,
        "template_score": 0.0,
        "conclusion": "",
    }
    if is_special_offers_screen(image, recognized_text):
        return diagnostic
    template_score = (
        0.0 if recognized_text is not None else _no_commanders_unique_template_score(image)
    )
    diagnostic["template_score"] = template_score
    strong_template = template_score >= 0.55
    if recognized_text is not None:
        text = recognized_text
    elif template_score < 0.48:
        return diagnostic
    else:
        text = _parchment_ocr(image)
    diagnostic["text"] = text
    text_hit = _no_commanders_text_hit(text)
    if not (text_hit or strong_template):
        return diagnostic
    if find_target_attack_button(image) is not None and not text_hit:
        return diagnostic
    if not _looks_like_hire_parchment(image):
        return diagnostic
    closes = _no_commanders_red_closes(image)
    point = (closes[0][1], closes[0][2]) if closes else None
    if point is None or is_green_hire_point(*point):
        return diagnostic
    diagnostic["point"] = point
    diagnostic["valid"] = True
    diagnostic["red_ratio"] = 1.0
    diagnostic["conclusion"] = _no_commanders_conclusion(text)
    return diagnostic


def is_no_commanders_parchment(image: Image.Image) -> bool:
    """Cheap template gate for wait-loops. Full OCR runs on dismiss."""
    return _no_commanders_unique_template_score(image) >= 0.48


def generic_modal_diagnostics(
    image: Image.Image,
    recognized_text: str | None = None,
) -> dict[str, Any]:
    """Return a red-X close only. Never buy/green; close спецпредложения with X."""
    diagnostic: dict[str, Any] = {
        "point": None,
        "action": None,
        "modal_bounds": None,
        "valid": False,
        "excluded": False,
        "special_offers": False,
    }
    text = (
        recognized_text
        if recognized_text is not None
        else ocr_text_ui(crop_rel(image, [0.05, 0.0, 0.95, 0.28]), psm=6)
    ).lower().replace("ё", "е")
    if is_special_offers_screen(image, text):
        point = special_offers_close_point(image)
        diagnostic.update(
            point=point,
            action="close",
            valid=True,
            special_offers=True,
        )
        return diagnostic
    currency_terms = (
        "купить",
        "покупк",
        "предложен",
        "магазин",
        "руб",
        "монет",
        "зол",
        "оплат",
    )
    if (
        is_formation_screen(image)
        or is_travel_dialog(image)
        or find_picker_cards(image)
        or find_target_attack_button(image) is not None
        or movement_confirm_diagnostics(image)["valid"]
        or any(term in text for term in currency_terms)
    ):
        diagnostic["excluded"] = True
        return diagnostic
    bgr = _reference_bgr(image)
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    beige = cv2.inRange(hsv, (8, 12, 95), (42, 190, 255))
    count, _, stats, _ = cv2.connectedComponentsWithStats(beige)
    panels: list[tuple[int, int, int, int, int]] = []
    for index in range(1, count):
        x, y, width, height, area = (int(value) for value in stats[index])
        if (
            area > 18000
            and 0.30 * 900 < width < 0.92 * 900
            and 0.10 * 1600 < height < 0.82 * 1600
            and 0.04 * 900 < x
            and x + width < 0.96 * 900
        ):
            panels.append((area, x, y, width, height))
    if not panels:
        return diagnostic
    _, px, py, pw, ph = max(panels)
    diagnostic["modal_bounds"] = (px / 900, py / 1600, (px + pw) / 900, (py + ph) / 1600)

    def components(mask: np.ndarray) -> list[tuple[int, float, float]]:
        count, _, stats, centers = cv2.connectedComponentsWithStats(mask)
        found: list[tuple[int, float, float]] = []
        for index in range(1, count):
            x, y, width, height, area = stats[index]
            cx, cy = centers[index]
            inside = px < cx < px + pw and py < cy < py + ph
            if not inside or area < 450:
                continue
            valid_position = cx > px + pw / 2 and cy < py + ph / 2
            valid_shape = 0.55 < width / max(1, height) < 1.8
            if valid_position and valid_shape:
                found.append((int(area), float(cx), float(cy)))
        return found

    red = cv2.bitwise_or(
        cv2.inRange(hsv, (0, 120, 75), (12, 255, 255)),
        cv2.inRange(hsv, (168, 120, 75), (180, 255, 255)),
    )
    red_actions = components(red)
    if red_actions:
        _, x, y = max(red_actions)
        nx, ny = x / 900, y / 1600
        if is_offer_rail_point(nx, ny):
            return diagnostic
        diagnostic.update(point=(nx, ny), action="close", valid=True)
    return diagnostic


def find_robber_candidates(
    image: Image.Image,
    threshold: float = 0.65,
    template_path: Path = ROBBER_TEMPLATE,
) -> list[tuple[float, float, float]]:
    if not template_path.exists() or not is_map_screen(image):
        return []
    bgr = _reference_bgr(image)
    template = cv2.imread(str(template_path))
    if template is None:
        return []
    ranked: list[tuple[float, int, int]] = []
    for scale in (0.55, 0.62, 0.70, 1.0):
        width = max(16, int(template.shape[1] * scale))
        height = max(16, int(template.shape[0] * scale))
        if width >= bgr.shape[1] or height >= bgr.shape[0]:
            continue
        resized = cv2.resize(template, (width, height), interpolation=cv2.INTER_AREA)
        scores = cv2.matchTemplate(bgr, resized, cv2.TM_CCOEFF_NORMED)
        ys, xs = np.where(scores >= float(threshold))
        half_w, half_h = width // 2, height // 2
        for y, x in zip(ys, xs):
            cx, cy = x + half_w, y + half_h
            if (
                0.18 * REFERENCE_SIZE[1] < cy < 0.88 * REFERENCE_SIZE[1]
                and 0.07 * REFERENCE_SIZE[0] < cx < OFFER_RAIL_X * REFERENCE_SIZE[0]
            ):
                ranked.append((float(scores[y, x]), int(cx), int(cy)))
    ranked.sort(reverse=True)
    selected: list[tuple[float, int, int]] = []
    min_distance = max(template.shape[:2]) * 0.55
    for score, x, y in ranked:
        if all((x - px) ** 2 + (y - py) ** 2 > min_distance**2 for _, px, py in selected):
            selected.append((score, x, y))
    return [
        (x / REFERENCE_SIZE[0], y / REFERENCE_SIZE[1], score)
        for score, x, y in selected
    ]


def is_burning_candidate(
    image: Image.Image,
    point: tuple[float, float],
) -> bool:
    """Conservatively identify the flame + smoke signature of cooldown towers."""
    rgb = np.asarray(image.convert("RGB"))
    hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)
    height, width = hsv.shape[:2]
    cx, cy = round(point[0] * width), round(point[1] * height)
    patch = hsv[
        max(0, cy - round(0.063 * height)) : min(height, cy + round(0.031 * height)),
        max(0, cx - round(0.056 * width)) : min(width, cx + round(0.056 * width)),
    ]
    if patch.size == 0:
        return True
    fire = (
        ((patch[:, :, 0] < 25) | (patch[:, :, 0] > 170))
        & (patch[:, :, 1] > 140)
        & (patch[:, :, 2] > 140)
    )
    smoke = (patch[:, :, 1] < 65) & (patch[:, :, 2] > 125)
    fire_ratio = float(np.mean(fire))
    smoke_ratio = float(np.mean(smoke))
    return fire_ratio > 0.021 or (fire_ratio > 0.015 and smoke_ratio > 0.04)


def find_main_castle_marker(image: Image.Image) -> tuple[float, float] | None:
    """Find the cyan banner used only by the current account's main castle."""
    bgr = _reference_bgr(image)
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(hsv, (75, 80, 70), (110, 255, 255))
    count, _, stats, centers = cv2.connectedComponentsWithStats(mask)
    candidates: list[tuple[int, float, float]] = []
    for index in range(1, count):
        x, y, width, height, area = stats[index]
        if area < 700 or width < 3.0 * max(1, height):
            continue
        cx, cy = centers[index]
        if 0.10 * 1600 < cy < 0.86 * 1600:
            candidates.append((area, cx, cy))
    if not candidates:
        return None
    _, x, y = max(candidates)
    return x / 900, y / 1600


def find_picker_cards(
    image: Image.Image,
    template_path: Path = PICKER_MAX_TEMPLATE,
    threshold: float = 0.65,
) -> list[dict[str, Any]]:
    if not template_path.exists():
        return []
    bgr = _reference_bgr(image)
    template = cv2.imread(str(template_path))
    if template is None:
        return []
    edges = cv2.Canny(cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY), 60, 140)
    template_edges = cv2.Canny(cv2.cvtColor(template, cv2.COLOR_BGR2GRAY), 60, 140)
    scores = cv2.matchTemplate(edges, template_edges, cv2.TM_CCOEFF_NORMED)
    ys, xs = np.where(scores >= threshold)
    half_w, half_h = template.shape[1] // 2, template.shape[0] // 2
    ranked = sorted(
        (
            (float(scores[y, x]), x + half_w, y + half_h)
            for y, x in zip(ys, xs)
            if 0.25 * REFERENCE_SIZE[0] < x + half_w < 0.50 * REFERENCE_SIZE[0]
            and 0.42 * REFERENCE_SIZE[1] < y + half_h < 0.82 * REFERENCE_SIZE[1]
        ),
        reverse=True,
    )
    selected: list[tuple[float, int, int]] = []
    for score, x, y in ranked:
        if all((x - px) ** 2 + (y - py) ** 2 > 70**2 for _, px, py in selected):
            selected.append((score, x, y))
    cards: list[dict[str, Any]] = []
    ref_image = Image.fromarray(cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB))
    for score, x, y in sorted(selected, key=lambda item: item[2]):
        ratio_region = [
            max(0.0, (x + 70) / 900),
            max(0.0, (y - 45) / 1600),
            min(1.0, (x + 345) / 900),
            min(1.0, (y + 45) / 1600),
        ]
        ratio = parse_ratio(ocr_text(crop_rel(ref_image, ratio_region), psm=6))
        if not ratio:
            continue
        cards.append(
            {
                "point": (x / 900, y / 1600),
                "selected": ratio[0],
                "available": ratio[1],
                "score": score,
                "fingerprint": f"{round(x / 20)}:{round(y / 20)}:{ratio[1]}",
            }
        )
    return cards


def find_picker_max_control(
    image: Image.Image,
    template_path: Path = PICKER_MAX_TEMPLATE,
    threshold: float = 0.65,
) -> tuple[float, float] | None:
    """LEFT-side MAX control that fills to capacity. Never the cancel that zeros."""
    if not template_path.exists():
        return None
    bgr = _reference_bgr(image)
    template = cv2.imread(str(template_path))
    if template is None:
        return None
    edges = cv2.Canny(cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY), 60, 140)
    template_edges = cv2.Canny(cv2.cvtColor(template, cv2.COLOR_BGR2GRAY), 60, 140)
    scores = cv2.matchTemplate(edges, template_edges, cv2.TM_CCOEFF_NORMED)
    _, score, _, location = cv2.minMaxLoc(scores)
    if score < threshold:
        return None
    x = location[0] + template.shape[1] / 2
    y = location[1] + template.shape[0] / 2
    nx, ny = x / REFERENCE_SIZE[0], y / REFERENCE_SIZE[1]
    if not (0.22 < nx < 0.50 and 0.42 < ny < 0.70):
        return None
    return nx, ny


def formation_wave_diagnostics(image: Image.Image) -> dict[str, Any]:
    """Locate wave headers and distinguish expanded from collapsed content."""
    bgr = _reference_bgr(image)
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    yellow = cv2.inRange(hsv, (15, 80, 105), (45, 255, 255))
    count, _, stats, _ = cv2.connectedComponentsWithStats(yellow)
    bands: list[tuple[int, int, int, int, int]] = []
    for index in range(1, count):
        x, y, width, height, area = (int(value) for value in stats[index])
        if width > 0.72 * 900 and 35 < height < 180 and area > 18000:
            bands.append((y, x, width, height, area))
    bands.sort()
    diagnostic: dict[str, Any] = {
        "expanded": False,
        "collapsed": False,
        "first_header": None,
        "content_bounds": None,
        "expand_point": None,
    }
    if not bands:
        return diagnostic
    y, x, width, height, _ = bands[0]
    diagnostic["first_header"] = (x / 900, y / 1600, (x + width) / 900, (y + height) / 1600)
    next_y = bands[1][0] if len(bands) > 1 else min(1500, y + 360)
    gap = next_y - (y + height)
    diagnostic["expanded"] = gap >= 140
    diagnostic["collapsed"] = gap < 140
    if diagnostic["expanded"]:
        diagnostic["content_bounds"] = (
            x / 900,
            (y + height) / 1600,
            (x + width) / 900,
            next_y / 1600,
        )
    else:
        diagnostic["expand_point"] = ((x + width - 35) / 900, (y + height / 2) / 1600)
    return diagnostic


def find_formation_unit_slots(image: Image.Image) -> list[tuple[float, float]]:
    """Find plus slots only inside validated expanded-wave content."""
    wave = formation_wave_diagnostics(image)
    if not wave["expanded"] or wave["content_bounds"] is None:
        return []
    left, top, right, bottom = wave["content_bounds"]
    bgr = _reference_bgr(image)
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    dark = cv2.inRange(hsv, (0, 0, 0), (35, 255, 105))
    y1, y2 = round(top * 1600), round(bottom * 1600)
    x1, x2 = round(left * 900), min(450, round(right * 900))
    roi = dark[y1:y2, x1:x2]
    count, _, stats, _ = cv2.connectedComponentsWithStats(roi)
    points: list[tuple[int, int]] = []
    for index in range(1, count):
        x, y, width, height, area = (int(value) for value in stats[index])
        if area < 260 or not (0.55 < width / max(1, height) < 1.8):
            continue
        patch = roi[y : y + height, x : x + width]
        mid_y, mid_x = height // 2, width // 2
        row = patch[max(0, mid_y - 4) : min(height, mid_y + 5), :]
        column = patch[:, max(0, mid_x - 4) : min(width, mid_x + 5)]
        if np.mean(row > 0) < 0.45 or np.mean(column > 0) < 0.45:
            continue
        points.append((x + width // 2 + x1, y + height // 2 + y1))
    selected: list[tuple[int, int]] = []
    for point in sorted(points, key=lambda item: (item[1], item[0])):
        if all((point[0] - px) ** 2 + (point[1] - py) ** 2 > 45**2 for px, py in selected):
            selected.append(point)
    return [(x / 900, y / 1600) for x, y in selected[:4]]


def find_target_attack_button(
    image: Image.Image,
    template_path: Path = TARGET_ATTACK_TEMPLATE,
    threshold: float = 0.72,
) -> tuple[float, float] | None:
    if not template_path.exists():
        return None
    bgr = _reference_bgr(image)
    template = cv2.imread(str(template_path))
    if template is None:
        return None
    result = cv2.matchTemplate(bgr, template, cv2.TM_CCOEFF_NORMED)
    _, score, _, location = cv2.minMaxLoc(result)
    if score < threshold:
        return None
    x = location[0] + template.shape[1] / 2
    y = location[1] + template.shape[0] / 2
    return x / 900, y / 1600


def picker_confirm_diagnostics(
    image: Image.Image,
    template_path: Path = PICKER_CONFIRM_TEMPLATE,
    threshold: float = 0.72,
    y_min: float = 0.70,
    y_max: float = 0.92,
) -> dict[str, Any]:
    """Green check in a vertical band. Picker confirm is lower-right (~0.73, 0.80)."""
    diagnostic: dict[str, Any] = {
        "point": None,
        "popup_bounds": (0.08, 0.18, 0.92, 0.90),
        "template_score": 0.0,
        "green_ratio": 0.0,
        "check_ratio": 0.0,
        "valid": False,
        "y_min": y_min,
        "y_max": y_max,
    }
    if not template_path.exists():
        return diagnostic
    bgr = _reference_bgr(image)
    template = cv2.imread(str(template_path))
    if template is None:
        return diagnostic
    result = cv2.matchTemplate(bgr, template, cv2.TM_CCOEFF_NORMED)
    _, score, _, location = cv2.minMaxLoc(result)
    diagnostic["template_score"] = float(score)
    if score < threshold:
        return diagnostic
    x = location[0] + template.shape[1] / 2
    y = location[1] + template.shape[0] / 2
    nx = x / REFERENCE_SIZE[0]
    ny = y / REFERENCE_SIZE[1]
    diagnostic["point"] = (nx, ny)
    if not (0.55 <= nx <= 0.95 and y_min <= ny <= y_max):
        return diagnostic
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    patch = hsv[
        max(0, round(y - 70)) : min(1600, round(y + 70)),
        max(0, round(x - 70)) : min(900, round(x + 70)),
    ]
    green = cv2.inRange(patch, (35, 80, 50), (95, 255, 255))
    check = cv2.inRange(patch, (0, 0, 185), (180, 85, 255))
    if not green.size:
        return diagnostic
    diagnostic["green_ratio"] = float(np.count_nonzero(green)) / green.size
    diagnostic["check_ratio"] = float(np.count_nonzero(check)) / check.size
    diagnostic["valid"] = (
        diagnostic["green_ratio"] >= 0.12 and diagnostic["check_ratio"] >= 0.015
    )
    return diagnostic


def find_picker_confirm_button(
    image: Image.Image,
    template_path: Path = PICKER_CONFIRM_TEMPLATE,
    threshold: float = 0.72,
) -> tuple[float, float] | None:
    diagnostic = picker_confirm_diagnostics(image, template_path, threshold)
    return diagnostic["point"] if diagnostic["valid"] else None


def find_empty_wave_warning_confirm(
    image: Image.Image,
    template_path: Path = PICKER_CONFIRM_TEMPLATE,
    threshold: float = 0.72,
) -> tuple[float, float] | None:
    """Mid-dialog green check for «не назначив солдат в волны», never the picker."""
    diagnostic = picker_confirm_diagnostics(
        image,
        template_path,
        threshold,
        y_min=0.48,
        y_max=0.68,
    )
    return diagnostic["point"] if diagnostic["valid"] else None


def movement_confirm_diagnostics(image: Image.Image) -> dict[str, Any]:
    """Locate the green movement submit button, never the red cancellation."""
    bgr = _reference_bgr(image)
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    green_mask = cv2.inRange(hsv, (35, 90, 45), (95, 255, 255))
    count, _, stats, _ = cv2.connectedComponentsWithStats(green_mask)
    diagnostic: dict[str, Any] = {
        "point": None,
        "dialog_bounds": (0.08, 0.14, 0.92, 0.88),
        "green_ratio": 0.0,
        "red_ratio": 0.0,
        "check_ratio": 0.0,
        "valid": False,
    }
    candidates: list[tuple[int, int, int, int, int]] = []
    for index in range(1, count):
        x, y, width, height, area = (int(value) for value in stats[index])
        cx, cy = x + width / 2, y + height / 2
        if (
            area >= 2500
            and width >= 1.5 * max(1, height)
            and cx > 0.50 * REFERENCE_SIZE[0]
            and cy > 0.68 * REFERENCE_SIZE[1]
        ):
            candidates.append((area, x, y, width, height))
    if not candidates:
        return diagnostic
    _, x, y, width, height = max(candidates)
    patch = hsv[y : y + height, x : x + width]
    green = cv2.inRange(patch, (35, 90, 45), (95, 255, 255))
    red_a = cv2.inRange(patch, (0, 100, 60), (12, 255, 255))
    red_b = cv2.inRange(patch, (168, 100, 60), (180, 255, 255))
    red = cv2.bitwise_or(red_a, red_b)
    check = cv2.inRange(patch, (0, 0, 190), (180, 85, 255))
    diagnostic["green_ratio"] = float(np.count_nonzero(green)) / green.size
    diagnostic["red_ratio"] = float(np.count_nonzero(red)) / red.size
    diagnostic["check_ratio"] = float(np.count_nonzero(check)) / check.size
    diagnostic["point"] = ((x + width / 2) / 900, (y + height / 2) / 1600)
    diagnostic["valid"] = (
        diagnostic["green_ratio"] >= 0.40
        and diagnostic["red_ratio"] <= 0.05
        and diagnostic["check_ratio"] >= 0.01
    )
    return diagnostic


def choose_shortest_candidate(
    measured: list[tuple[tuple[float, float], int]],
) -> tuple[float, float] | None:
    valid = [(point, seconds) for point, seconds in measured if seconds > 0]
    return min(valid, key=lambda item: item[1])[0] if valid else None


def _movement_tile_score(image: Image.Image, nx: float, ny: float) -> float:
    crop = crop_rel(
        image,
        [
            max(0.0, nx - 0.09),
            max(0.0, ny - 0.07),
            min(1.0, nx + 0.09),
            min(1.0, ny + 0.07),
        ],
    )
    arr = np.asarray(crop)
    if arr.size == 0:
        return 0.0
    red = arr[:, :, 0].astype(np.float32)
    green = arr[:, :, 1].astype(np.float32)
    blue = arr[:, :, 2].astype(np.float32)
    goldish = ((red > 170) & (green > 130) & (blue < 120)).mean()
    selected_green = ((green > red + 15) & (green > 140) & (blue < 140)).mean()
    bright = float(arr.mean()) / 255.0
    return float(goldish * 2.2 + selected_green * 1.6 + bright)


def is_feather_selected(
    image: Image.Image,
    feather: tuple[float, float] = (0.80, 0.34),
    gold: tuple[float, float] = (0.20, 0.34),
) -> bool:
    """True when the right-hand feather tile looks selected vs the gold tile."""
    feather_score = _movement_tile_score(image, feather[0], feather[1])
    gold_score = _movement_tile_score(image, gold[0], gold[1])
    return feather_score > gold_score + 0.035


def choose_movement(feather_count: int | None) -> str:
    if feather_count is None:
        return "unknown"
    return "feather" if feather_count > 0 else "gold"


def flank_fill_allowed(filled: int, capacity: int, minimum: float = 0.70) -> bool:
    return capacity > 0 and filled / capacity >= minimum


def find_template_center(
    image: Image.Image,
    template_path: Path,
    threshold: float = 0.72,
    x_min: float = 0.0,
    x_max: float = 1.0,
    y_min: float = 0.0,
    y_max: float = 1.0,
    scales: tuple[float, ...] = (0.45, 0.55, 0.7, 0.85, 1.0, 1.2, 1.5, 2.0),
) -> tuple[float, float] | None:
    """Best template match as normalized center, or None."""
    if not template_path.exists():
        return None
    template = cv2.imread(str(template_path))
    if template is None or template.size == 0:
        return None
    bgr = _reference_bgr(image)
    ih, iw = bgr.shape[:2]
    th, tw = template.shape[:2]
    best_score = float(threshold)
    best: tuple[float, float] | None = None
    for scale in scales:
        width = max(12, int(tw * scale))
        height = max(12, int(th * scale))
        if width >= iw or height >= ih:
            continue
        resized = cv2.resize(template, (width, height), interpolation=cv2.INTER_AREA)
        scores = cv2.matchTemplate(bgr, resized, cv2.TM_CCOEFF_NORMED)
        _, score, _, loc = cv2.minMaxLoc(scores)
        nx = (loc[0] + width / 2) / iw
        ny = (loc[1] + height / 2) / ih
        if score >= best_score and x_min <= nx <= x_max and y_min <= ny <= y_max:
            best_score = float(score)
            best = (nx, ny)
    return best


def is_loading_screen(image: Image.Image) -> bool:
    """Server connect / splash — wait, do not tap shop or search."""
    if is_map_screen(image):
        return False
    if is_special_offers_screen(image) or is_formation_screen(image) or is_travel_dialog(image):
        return False
    if is_connection_error_dialog(image):
        return False
    text = _ocr_compact(ocr_text_ui(crop_rel(image, [0.08, 0.68, 0.92, 0.92]), psm=6))
    markers = (
        "подключ",
        "podkluch",
        "connecting",
        "игровомусервер",
        "сервер",
        "cepbep",
        "cephep",
        "pobomycep",
        "flogknrou",
    )
    if any(token in text for token in markers):
        return True
    bgr = _reference_bgr(image)
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    h, w = hsv.shape[:2]
    # Splash art is not grassy map; require low map-green before trusting the bar.
    field = hsv[int(0.20 * h) : int(0.75 * h), int(0.08 * w) : int(0.92 * w)]
    map_green = cv2.inRange(field, (30, 45, 35), (95, 255, 255))
    if float(np.count_nonzero(map_green)) / max(1, map_green.size) > 0.18:
        return False
    band = hsv[int(0.86 * h) : int(0.96 * h), int(0.08 * w) : int(0.92 * w)]
    purple = cv2.inRange(band, (120, 40, 40), (160, 255, 255))
    if float(np.count_nonzero(purple)) / max(1, purple.size) > 0.18:
        return True
    red = cv2.bitwise_or(
        cv2.inRange(band, (0, 80, 80), (15, 255, 255)),
        cv2.inRange(band, (165, 80, 80), (180, 255, 255)),
    )
    return float(np.count_nonzero(red)) / max(1, red.size) > 0.18


def is_connection_error_dialog(image: Image.Image) -> bool:
    """Splash «Ошибка соединения» with SUPPORT / RECONNECT — never map/formation."""
    if is_map_screen(image) or is_formation_screen(image) or is_travel_dialog(image):
        return False
    if is_special_offers_screen(image) or is_taxes_dialog(image):
        return False
    # Splash is not grassy and not parchment-brown.
    bgr = _reference_bgr(image)
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    field = hsv[int(0.18 * REFERENCE_SIZE[1]) : int(0.72 * REFERENCE_SIZE[1]), 50:850]
    green = cv2.inRange(field, (30, 45, 35), (95, 255, 255))
    brown = cv2.inRange(field, (3, 45, 20), (30, 255, 180))
    if float(np.count_nonzero(green)) / max(1, green.size) > 0.16:
        return False
    if float(np.count_nonzero(brown)) / max(1, brown.size) > 0.14:
        return False
    mask = cv2.inRange(hsv, (0, 0, 35), (180, 55, 130))
    count, _, stats, _ = cv2.connectedComponentsWithStats(mask)
    has_gray = False
    for index in range(1, count):
        x, y, width, height, area = stats[index]
        if area < 80000 or width < 400 or height < 160:
            continue
        cx, cy = x + width / 2, y + height / 2
        if 0.25 * REFERENCE_SIZE[0] < cx < 0.75 * REFERENCE_SIZE[0] and 0.30 * REFERENCE_SIZE[1] < cy < 0.70 * REFERENCE_SIZE[1]:
            has_gray = True
            break
    if not has_gray:
        return False
    text = _ocr_compact(ocr_text_ui(crop_rel(image, [0.12, 0.28, 0.88, 0.62]), psm=6))
    markers = (
        "ошибкасоединен",
        "связьссервер",
        "соединитьповтор",
        "oshibkasoed",
        "podderzh",
        "tryagain",
        "прерван",
    )
    # Accept gray splash dialog even when OCR is garbled: no map/formation chrome.
    return True if any(token in text for token in markers) else has_gray


def find_reconnect_button(image: Image.Image) -> tuple[float, float]:
    """Right primary action of the connection-error dialog."""
    bgr = _reference_bgr(image)
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(hsv, (0, 0, 35), (180, 55, 130))
    count, _, stats, _ = cv2.connectedComponentsWithStats(mask)
    best: tuple[int, int, int, int, int] | None = None
    for index in range(1, count):
        x, y, width, height, area = (int(v) for v in stats[index])
        if area < 80000 or width < 400 or height < 160:
            continue
        cx, cy = x + width / 2, y + height / 2
        if not (
            0.25 * REFERENCE_SIZE[0] < cx < 0.75 * REFERENCE_SIZE[0]
            and 0.30 * REFERENCE_SIZE[1] < cy < 0.70 * REFERENCE_SIZE[1]
        ):
            continue
        if best is None or area > best[0]:
            best = (area, x, y, width, height)
    if best is None:
        return (0.72, 0.55)
    _, x, y, width, height = best
    return ((x + width * 0.78) / REFERENCE_SIZE[0], (y + height * 0.78) / REFERENCE_SIZE[1])


def is_hire_menu(image: Image.Image) -> bool:
    """Parchment «Нанять» / ally-help tabs. Never the no-commanders ruby hire seal alone."""
    if is_formation_screen(image) or is_travel_dialog(image) or is_special_offers_screen(image):
        return False
    top = _ocr_compact(ocr_text_ui(crop_rel(image, [0.12, 0.00, 0.88, 0.16]), psm=6))
    markers = ("нанять", "nanvat", "nanatb", "nomow", "помощьсоюзник", "pomoshsoyuz", "hire")
    return any(token in top for token in markers)


def find_reward_confirm(image: Image.Image) -> tuple[float, float] | None:
    """Green claim check (галочка) on reward popups. Never shop/view-offer, never hire seal."""
    if is_loading_screen(image) or is_connection_error_dialog(image):
        return None
    bgr = _reference_bgr(image)
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    green = cv2.inRange(hsv, (35, 90, 50), (95, 255, 255))
    count, _, stats, centers = cv2.connectedComponentsWithStats(green)
    for index in range(1, count):
        x, y, width, height, area = (int(v) for v in stats[index])
        ratio = width / max(1, height)
        nx = float(centers[index][0]) / REFERENCE_SIZE[0]
        ny = float(centers[index][1]) / REFERENCE_SIZE[1]
        if not (
            3500 <= area <= 90000
            and 2.4 <= ratio <= 3.5
            and 0.40 <= nx <= 0.60
            and 0.62 <= ny <= 0.78
        ):
            continue
        patch = hsv[y : y + height, x : x + width]
        check = cv2.inRange(patch, (0, 0, 185), (180, 85, 255))
        if float(np.count_nonzero(check)) / max(1, check.size) >= 0.025:
            return (nx, ny)
    height, width = hsv.shape[:2]
    ribbon = hsv[
        int(0.43 * height) : int(0.58 * height),
        int(0.05 * width) : int(0.95 * width),
    ]
    ribbon_red = cv2.bitwise_or(
        cv2.inRange(ribbon, (0, 80, 60), (18, 255, 255)),
        cv2.inRange(ribbon, (165, 80, 60), (180, 255, 255)),
    )
    if float(np.count_nonzero(ribbon_red)) / max(1, ribbon_red.size) >= 0.12:
        event_check = picker_confirm_diagnostics(image, y_min=0.55, y_max=0.80)
        if event_check["valid"] and event_check["point"] is not None:
            return (
                float(event_check["point"][0]),
                float(event_check["point"][1]),
            )
    if is_special_offers_screen(image):
        return None
    if is_formation_screen(image) or is_travel_dialog(image):
        return None
    if is_no_commanders_parchment(image):
        return None
    diagnostic = picker_confirm_diagnostics(image, y_min=0.52, y_max=0.90)
    point = diagnostic["point"] if diagnostic.get("valid") else None
    if point is not None:
        nx, ny = float(point[0]), float(point[1])
        if 0.28 <= nx <= 0.78 and not is_green_hire_point(nx, ny):
            return (nx, ny)
        if 0.28 <= nx <= 0.72:
            return (nx, ny)
    best: tuple[float, float, float] | None = None
    for index in range(1, count):
        x, y, width, height, area = (int(v) for v in stats[index])
        if area < 3500 or area > 90000:
            continue
        ratio = width / max(1, height)
        if not (0.55 <= ratio <= 3.5):
            continue
        nx = float(centers[index][0]) / REFERENCE_SIZE[0]
        ny = float(centers[index][1]) / REFERENCE_SIZE[1]
        if not (0.28 <= nx <= 0.78 and 0.52 <= ny <= 0.90):
            continue
        if is_offer_rail_point(nx, ny):
            continue
        patch = hsv[y : y + height, x : x + width]
        check = cv2.inRange(patch, (0, 0, 185), (180, 85, 255))
        check_ratio = float(np.count_nonzero(check)) / max(1, check.size)
        if check_ratio < 0.015:
            continue
        score = area + (1.0 - abs(nx - 0.50)) * 8000 + (1.0 - abs(ny - 0.75)) * 4000
        if best is None or score > best[0]:
            best = (score, nx, ny)
    if best is None:
        return None
    return (best[1], best[2])


def is_reward_claim_popup(image: Image.Image) -> bool:
    return find_reward_confirm(image) is not None


def is_event_reward_popup(image: Image.Image) -> bool:
    """Recognize the event-reward parchment without confusing ruby commander hire."""
    bgr = _reference_bgr(image)
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    height, width = hsv.shape[:2]
    ribbon = hsv[
        int(0.43 * height) : int(0.58 * height),
        int(0.05 * width) : int(0.95 * width),
    ]
    red = cv2.bitwise_or(
        cv2.inRange(ribbon, (0, 80, 60), (18, 255, 255)),
        cv2.inRange(ribbon, (165, 80, 60), (180, 255, 255)),
    )
    if float(np.count_nonzero(red)) / max(1, red.size) < 0.12:
        return False
    return bool(
        picker_confirm_diagnostics(image, y_min=0.55, y_max=0.80).get("valid")
    )


def is_presets_dialog(image: Image.Image) -> bool:
    text = _ocr_compact(ocr_text_ui(crop_rel(image, [0.05, 0.00, 0.95, 0.32]), psm=6))
    markers = (
        "предустанов",
        "predustanov",
        "npelyctahob",
        "npeayctahob",
        "npeaycta",
    )
    if any(token in text for token in markers):
        return True
    return find_template_center(image, PRESETS_DIALOG_TEMPLATE, threshold=0.36) is not None


def is_autoselect_dialog(image: Image.Image) -> bool:
    text = _ocr_compact(ocr_text_ui(crop_rel(image, [0.05, 0.00, 0.95, 0.22]), psm=6))
    markers = (
        "автоподбор",
        "avtopodbor",
        "abtonodbor",
        "avtonod",
        "abtonod",
        "autoselect",
    )
    if any(token in text for token in markers):
        return True
    return find_template_center(image, AUTOSELECT_DIALOG_TEMPLATE, threshold=0.36) is not None


def find_preset_button(image: Image.Image) -> tuple[float, float] | None:
    return find_template_center(
        image,
        PRESET_BUTTON_TEMPLATE,
        threshold=0.60,
        y_min=0.82,
        y_max=0.98,
        x_max=0.55,
    )


def find_autoselect_button(image: Image.Image) -> tuple[float, float] | None:
    return find_template_center(
        image,
        AUTOSELECT_BUTTON_TEMPLATE,
        threshold=0.48,
        y_min=0.88,
        y_max=0.99,
        x_max=0.22,
    )


def find_apply_preset_all(image: Image.Image) -> tuple[float, float] | None:
    return find_template_center(
        image,
        APPLY_PRESET_ALL_TEMPLATE,
        threshold=0.62,
        y_min=0.50,
        y_max=0.98,
    )


def find_preset_dialog_close(image: Image.Image) -> tuple[float, float] | None:
    """Red X of the presets dialog, never the attack-plan title-bar close."""
    point = find_red_cross_force(image, title_bar_only=False)
    if point and 0.62 < point[0] < 0.98 and 0.02 < point[1] < 0.32:
        if not (point[0] > 0.90 and point[1] < 0.055):
            return point
    fallback = find_template_center(
        image,
        PRESETS_DIALOG_TEMPLATE,
        threshold=0.50,
        x_min=0.70,
        y_min=0.06,
        y_max=0.28,
    )
    if fallback:
        return (min(0.92, fallback[0] + 0.28), max(0.10, fallback[1] - 0.22))
    return (0.88, 0.16)


def find_autoselect_confirm(image: Image.Image) -> tuple[float, float] | None:
    """Bottom-right green check of «Автоподбор волн», never a mid-dialog warning."""
    diagnostic = movement_confirm_diagnostics(image)
    point = diagnostic.get("point")
    if diagnostic.get("valid") and point is not None and point[0] > 0.55 and point[1] > 0.70:
        return point
    return (0.77, 0.82)


def find_tool_bonus_candidates(image: Image.Image) -> list[tuple[int, float, float]]:
    """Support tools that show a red +% mark. Lower percent is better. Never the ruby cart."""
    if not TOOL_BONUS_TEMPLATE.exists():
        return []
    bgr = _reference_bgr(image)
    rgb = Image.fromarray(cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB))
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    red = cv2.bitwise_or(
        cv2.inRange(hsv, (0, 90, 110), (18, 255, 255)),
        cv2.inRange(hsv, (165, 90, 110), (180, 255, 255)),
    )
    # The asset's distinctive mark is the wide red Samurai-token target.
    # Restricting detection to its effect column excludes XP/coin icons and ruby carts.
    x1, x2 = int(0.62 * REFERENCE_SIZE[0]), int(0.74 * REFERENCE_SIZE[0])
    y1, y2 = int(0.32 * REFERENCE_SIZE[1]), int(0.78 * REFERENCE_SIZE[1])
    strip = np.zeros_like(red)
    strip[y1:y2, x1:x2] = red[y1:y2, x1:x2]
    strip = cv2.morphologyEx(strip, cv2.MORPH_CLOSE, np.ones((7, 7), np.uint8))
    strip = cv2.dilate(strip, np.ones((3, 3), np.uint8))
    count, _, stats, centers = cv2.connectedComponentsWithStats(strip)
    found: list[tuple[int, float, float]] = []
    for index in range(1, count):
        _x, _y, width, height, area = stats[index]
        if not (1500 <= area <= 4000 and 50 <= width <= 90 and 45 <= height <= 75):
            continue
        nx = float(centers[index][0] / REFERENCE_SIZE[0])
        ny = float(centers[index][1] / REFERENCE_SIZE[1])
        row = [0.20, max(0.30, ny - 0.07), 0.88, min(0.82, ny + 0.05)]
        percent = parse_bonus_percent(ocr_text_ui(crop_rel(rgb, row), psm=6))
        if percent is None:
            continue
        # Slider plus is one effect-line above the token icon.
        found.append((int(percent), nx, ny - 0.04))
    return sorted(found, key=lambda row: (row[0], row[2]))


def find_tool_slider_plus(image: Image.Image, row_y: float) -> tuple[float, float] | None:
    """White + on the selected tool slider. Never the ruby shopping cart below."""
    bgr = _reference_bgr(image)
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    y1 = int(max(0.32, row_y - 0.04) * REFERENCE_SIZE[1])
    y2 = int(min(0.74, row_y + 0.04) * REFERENCE_SIZE[1])
    x1 = int(0.72 * REFERENCE_SIZE[0])
    x2 = int(0.84 * REFERENCE_SIZE[0])
    if y2 <= y1:
        return (0.80, min(0.72, row_y))
    white = cv2.inRange(hsv[y1:y2, x1:x2], (0, 0, 170), (180, 50, 255))
    count, _, stats, centers = cv2.connectedComponentsWithStats(white)
    best: tuple[int, float, float] | None = None
    for index in range(1, count):
        _x, _y, width, height, area = stats[index]
        if not (30 <= area <= 1500 and 6 <= width <= 60 and 6 <= height <= 60):
            continue
        nx = (centers[index][0] + x1) / REFERENCE_SIZE[0]
        ny = (centers[index][1] + y1) / REFERENCE_SIZE[1]
        if ny > 0.74:
            continue
        if best is None or area > best[0]:
            best = (int(area), nx, ny)
    if best:
        return (best[1], best[2])
    return (0.80, min(0.72, row_y))


def find_samurai_candidates(
    image: Image.Image,
    threshold: float = 0.65,
) -> list[tuple[float, float, float]]:
    """Up to four camps: template-match samurai_camp.png (green grass masked)."""
    if not SAMURAI_TEMPLATE.exists() or not is_map_screen(image):
        return []
    template = cv2.imread(str(SAMURAI_TEMPLATE))
    if template is None:
        return []
    bgr = _reference_bgr(image)
    hsv = cv2.cvtColor(template, cv2.COLOR_BGR2HSV)
    mask = cv2.bitwise_not(cv2.inRange(hsv, (28, 35, 25), (95, 255, 255)))
    ranked: list[tuple[float, int, int]] = []
    # Isolated sprite is ~1.0; in-game camps around the castle are ~1.15.
    score_min = max(0.90, float(threshold) - 0.03)
    for scale in (1.0, 1.15, 1.35, 1.55, 1.75):
        width = max(20, int(template.shape[1] * scale))
        height = max(20, int(template.shape[0] * scale))
        if width >= bgr.shape[1] or height >= bgr.shape[0]:
            continue
        resized = cv2.resize(template, (width, height), interpolation=cv2.INTER_AREA)
        resized_mask = cv2.resize(mask, (width, height), interpolation=cv2.INTER_NEAREST)
        _, resized_mask = cv2.threshold(resized_mask, 127, 255, cv2.THRESH_BINARY)
        try:
            scores = cv2.matchTemplate(
                bgr, resized, cv2.TM_CCORR_NORMED, mask=resized_mask
            )
        except cv2.error:
            continue
        ys, xs = np.where(scores >= score_min)
        half_w, half_h = width // 2, height // 2
        for y, x in zip(ys, xs):
            cx, cy = int(x + half_w), int(y + half_h)
            nx, ny = cx / REFERENCE_SIZE[0], cy / REFERENCE_SIZE[1]
            if not (
                0.07 < nx < OFFER_RAIL_X
                and 0.16 < ny < 0.82
            ):
                continue
            patch = bgr[
                max(0, cy - 36) : cy + 36,
                max(0, cx - 36) : cx + 36,
            ]
            if patch.size == 0:
                continue
            ph = cv2.cvtColor(patch, cv2.COLOR_BGR2HSV)
            red = cv2.bitwise_or(
                cv2.inRange(ph, (0, 80, 80), (12, 255, 255)),
                cv2.inRange(ph, (168, 80, 80), (180, 255, 255)),
            )
            brown = cv2.inRange(ph, (5, 40, 40), (25, 200, 180))
            if float(np.mean(red)) / 255 < 0.008 or float(np.mean(brown)) / 255 < 0.03:
                continue
            ranked.append((float(scores[y, x]), cx, cy))
    ranked.sort(reverse=True)
    selected: list[tuple[float, int, int]] = []
    min_distance = 48
    for score, x, y in ranked:
        if all((x - px) ** 2 + (y - py) ** 2 > min_distance**2 for _, px, py in selected):
            selected.append((score, x, y))
        if len(selected) >= 4:
            break
    return [
        (x / REFERENCE_SIZE[0], y / REFERENCE_SIZE[1], score)
        for score, x, y in selected
    ]


def parse_samurai_camp_level(text: str) -> int | None:
    """Read camp level from popup OCR (Ур. 41, 31, 101, …)."""
    if not text:
        return None
    compact = text.lower().replace("ё", "е")
    labeled = re.search(
        r"(?:ур(?:овень)?|lvl|lv\.?|level)[^\d]{0,8}(\d{2,3})",
        compact,
        flags=re.IGNORECASE,
    )
    if labeled:
        return int(labeled.group(1))
    return None


def remaining_attacks_from_level(level: int) -> int:
    """11 - last digit; last digit 0 → 1 remaining. Level *1 → 10 attacks left."""
    last = abs(int(level)) % 10
    return 1 if last == 0 else 11 - last


def is_difficulty_dialog(image: Image.Image) -> bool:
    text = _ocr_compact(ocr_text_ui(crop_rel(image, [0.08, 0.03, 0.92, 0.24]), psm=6))
    markers = (
        "выберитесложност",
        "выберисложност",
        "сложност",
        "slozhnost",
        "difficulty",
        "ветеран",
        "veteran",
    )
    return any(token in text for token in markers)


def find_difficulty_lock_rows(image: Image.Image) -> list[float]:
    """Normalized Y of padlocks on the right of difficulty rows, top to bottom."""
    bgr = _reference_bgr(image)
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    gold = cv2.inRange(hsv, (8, 60, 80), (38, 255, 255))
    grey = cv2.inRange(hsv, (0, 0, 70), (50, 80, 210))
    locks = cv2.bitwise_or(gold, grey)
    x1, x2 = int(0.58 * REFERENCE_SIZE[0]), int(0.90 * REFERENCE_SIZE[0])
    y1, y2 = int(0.16 * REFERENCE_SIZE[1]), int(0.80 * REFERENCE_SIZE[1])
    strip = np.zeros_like(locks)
    strip[y1:y2, x1:x2] = locks[y1:y2, x1:x2]
    count, _, stats, centers = cv2.connectedComponentsWithStats(strip)
    rows: list[float] = []
    for index in range(1, count):
        _x, _y, width, height, area = stats[index]
        if not (70 <= area <= 1400 and 8 <= width <= 58 and 10 <= height <= 58):
            continue
        ny = float(centers[index][1] / REFERENCE_SIZE[1])
        if all(abs(ny - other) > 0.035 for other in rows):
            rows.append(ny)
    rows.sort()
    return rows


def pick_open_difficulty_point(image: Image.Image) -> tuple[float, float] | None:
    """Row above the topmost lock — highest difficulty still unlocked."""
    locks = find_difficulty_lock_rows(image)
    if not locks:
        return None
    open_y = min(locks) - 0.085
    return (0.50, min(0.72, max(0.20, open_y)))


def is_transient_error_dialog(image: Image.Image) -> bool:
    """Server warning like 2032 — dismiss, do not tap shop/search."""
    text = _ocr_compact(ocr_text_ui(crop_rel(image, [0.12, 0.22, 0.88, 0.58]), psm=6))
    markers = (
        "предупрежд",
        "произошлаошибк",
        "попытайсяеще",
        "2032",
        "npeqynpe",
        "peeytip",
        "oun6ka",
    )
    return any(token in text for token in markers)


def find_transient_error_confirm(image: Image.Image) -> tuple[float, float] | None:
    """Center green OK of a warning modal, never ruby-hire and never offer rail."""
    bgr = _reference_bgr(image)
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    green = cv2.inRange(hsv, (35, 90, 45), (95, 255, 255))
    count, _, stats, centers = cv2.connectedComponentsWithStats(green)
    best: tuple[int, float, float] | None = None
    for index in range(1, count):
        x, y, width, height, area = stats[index]
        if area < 4000 or width < 100 or height < 40:
            continue
        cx, cy = float(centers[index][0]), float(centers[index][1])
        nx, ny = cx / REFERENCE_SIZE[0], cy / REFERENCE_SIZE[1]
        if not (0.32 < nx < 0.68 and 0.48 < ny < 0.72):
            continue
        if is_green_hire_point(nx, ny) and nx > 0.58:
            continue
        if best is None or area > best[0]:
            best = (area, nx, ny)
    if best:
        return (best[1], best[2])
    return (0.50, 0.63)


