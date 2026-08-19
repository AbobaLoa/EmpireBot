from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import cv2
import numpy as np
from PIL import Image

from e4kbot.paths import ROOT

REFERENCE_SIZE = (900, 1600)
ROBBER_TEMPLATE = ROOT / "assets" / "robber_castle.png"
PICKER_MAX_TEMPLATE = ROOT / "assets" / "picker_max.png"
TARGET_ATTACK_TEMPLATE = ROOT / "assets" / "target_attack.png"
PICKER_CONFIRM_TEMPLATE = ROOT / "assets" / "picker_confirm.png"


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
    try:
        import pytesseract

        default_binary = Path(r"C:\Program Files\Tesseract-OCR\tesseract.exe")
        if default_binary.exists():
            pytesseract.pytesseract.tesseract_cmd = str(default_binary)
        gray = cv2.cvtColor(np.asarray(image.convert("RGB")), cv2.COLOR_RGB2GRAY)
        gray = cv2.resize(gray, None, fx=2.5, fy=2.5, interpolation=cv2.INTER_CUBIC)
        gray = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1]
        return pytesseract.image_to_string(
            gray,
            config=f"--psm {psm} -c tessedit_char_whitelist=0123456789:/",
        ).strip()
    except Exception:
        return ""


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


def is_formation_screen(image: Image.Image) -> bool:
    bgr = _reference_bgr(image)
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    brown = cv2.inRange(hsv[0:940], (3, 45, 20), (30, 255, 180))
    yellow = cv2.inRange(hsv[880:1040], (15, 90, 120), (45, 255, 255))
    return (
        float(np.count_nonzero(brown)) / brown.size > 0.18
        and float(np.count_nonzero(yellow)) / yellow.size > 0.10
    )


def is_travel_dialog(image: Image.Image) -> bool:
    bgr = _reference_bgr(image)
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    brown = cv2.inRange(hsv[230:1360, 70:830], (3, 35, 20), (30, 255, 190))
    green = cv2.inRange(hsv[1240:1380, 500:840], (35, 80, 50), (95, 255, 255))
    return (
        float(np.count_nonzero(brown)) / brown.size > 0.32
        and float(np.count_nonzero(green)) / green.size > 0.10
    )


def popup_action(image: Image.Image) -> tuple[float, float] | None:
    """Return a safe close/continue point for a blocking modal."""
    if is_formation_screen(image) or is_travel_dialog(image):
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
        if cx > 0.70 * REFERENCE_SIZE[0] and cy < 0.38 * REFERENCE_SIZE[1]:
            close_candidates.append((area, cx, cy))
    if close_candidates:
        _, x, y = max(close_candidates)
        return x / REFERENCE_SIZE[0], y / REFERENCE_SIZE[1]

    green = cv2.inRange(hsv, (35, 80, 70), (95, 255, 255))
    count, _, stats, centers = cv2.connectedComponentsWithStats(green)
    buttons: list[tuple[int, float, float]] = []
    for index in range(1, count):
        x, y, width, height, area = stats[index]
        if area < 1500 or width < 2.2 * height:
            continue
        cx, cy = centers[index]
        if 0.25 * REFERENCE_SIZE[0] < cx < 0.75 * REFERENCE_SIZE[0] and cy > 0.35 * REFERENCE_SIZE[1]:
            buttons.append((area, cx, cy))
    if buttons:
        _, x, y = max(buttons)
        return x / REFERENCE_SIZE[0], y / REFERENCE_SIZE[1]
    return None


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
    edges = cv2.Canny(cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY), 60, 140)
    template_edges = cv2.Canny(cv2.cvtColor(template, cv2.COLOR_BGR2GRAY), 60, 140)
    scores = cv2.matchTemplate(edges, template_edges, cv2.TM_CCOEFF_NORMED)
    ys, xs = np.where(scores >= float(threshold))
    half_w, half_h = template.shape[1] // 2, template.shape[0] // 2
    ranked = sorted(
        (
            (float(scores[y, x]), x + half_w, y + half_h)
            for y, x in zip(ys, xs)
            if 0.18 * REFERENCE_SIZE[1] < y + half_h < 0.88 * REFERENCE_SIZE[1]
            and 0.07 * REFERENCE_SIZE[0] < x + half_w < 0.93 * REFERENCE_SIZE[0]
        ),
        reverse=True,
    )
    selected: list[tuple[float, int, int]] = []
    min_distance = max(template.shape[:2]) * 0.75
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
) -> dict[str, Any]:
    diagnostic: dict[str, Any] = {
        "point": None,
        "popup_bounds": (0.08, 0.18, 0.92, 0.90),
        "template_score": 0.0,
        "green_ratio": 0.0,
        "check_ratio": 0.0,
        "valid": False,
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
    left, top, right, bottom = diagnostic["popup_bounds"]
    if not (
        (left + right) / 2 < x / 900 < right
        and (top + bottom) / 2 < y / 1600 < bottom
    ):
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
    diagnostic["point"] = (x / 900, y / 1600)
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


def choose_movement(feather_count: int | None) -> str:
    if feather_count is None:
        return "unknown"
    return "feather" if feather_count > 0 else "gold"


def flank_fill_allowed(filled: int, capacity: int, minimum: float = 0.70) -> bool:
    return capacity > 0 and filled / capacity >= minimum
