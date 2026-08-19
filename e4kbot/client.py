from __future__ import annotations

import json
import random
import re
import time
from pathlib import Path
from typing import Any

from loguru import logger

from e4kbot.bluestacks import AdbClient, capture_game_image, save_shot
from e4kbot.paths import LAYOUTS_DIR, ROOT
from e4kbot.safety import commander_number_ok, concurrent_ok, tap_jitter, triangular_delay
from e4kbot.state import StateStore
from e4kbot.telegram_bot import TelegramReporter

try:
    import pytesseract
except Exception:  # pragma: no cover
    pytesseract = None


def load_layout(name: str) -> dict[str, Any]:
    path = LAYOUTS_DIR / f"{name}.json"
    if not path.exists():
        path = LAYOUTS_DIR / "default.json"
    return json.loads(path.read_text(encoding="utf-8"))


def load_targets() -> dict[str, list[dict[str, int]]]:
    path = ROOT / "targets.json"
    if not path.exists():
        return {"barons": [], "nomads": [], "shogun": []}
    return json.loads(path.read_text(encoding="utf-8"))


def _abs_point(image_size: tuple[int, int], rel: list[float]) -> tuple[int, int]:
    w, h = image_size
    return int(rel[0] * w), int(rel[1] * h)


def _crop(image: Any, rel: list[float]) -> Any:
    w, h = image.size
    x1, y1, x2, y2 = rel
    return image.crop((int(x1 * w), int(y1 * h), int(x2 * w), int(y2 * h)))


def _ocr(image: Any) -> str:
    if pytesseract is None:
        return ""
    try:
        return pytesseract.image_to_string(image, config="--psm 7")
    except Exception:
        return ""


def parse_commander_number(text: str) -> int | None:
    match = re.search(r"(\d{1,2})", text.replace("O", "0"))
    if not match:
        return None
    return int(match.group(1))


def parse_march_seconds(text: str) -> int | None:
    text = text.strip()
    match = re.search(r"(\d{1,2})[^\d]+(\d{2})[^\d]+(\d{2})", text)
    if match:
        h, m, s = (int(match.group(1)), int(match.group(2)), int(match.group(3)))
        return h * 3600 + m * 60 + s
    match = re.search(r"(\d{1,2})[^\d]+(\d{2})", text)
    if match:
        return int(match.group(1)) * 60 + int(match.group(2))
    return None


class BlueStacksEngine:
    def __init__(
        self,
        config: dict[str, Any],
        store: StateStore,
        telegram: TelegramReporter,
        adb: AdbClient,
    ) -> None:
        self.config = config
        self.store = store
        self.telegram = telegram
        self.adb = adb
        self.layout = load_layout(str((config.get("bluestacks") or {}).get("layout") or "default"))
        self._next_commander = 1

    def _size(self) -> tuple[int, int]:
        image = capture_game_image(self.config, self.adb)
        if image is None:
            raise RuntimeError("Нет скрина BlueStacks — открой игру")
        return image.size

    def tap_rel(self, key: str) -> None:
        point = self.layout["buttons"][key]
        self._tap_norm(float(point[0]), float(point[1]))

    def _tap_norm(self, nx: float, ny: float) -> None:
        size = self._size()
        x, y = _abs_point(size, [nx, ny])
        jx, jy = tap_jitter(5)
        self.adb.tap(x + jx, y + jy)
        time.sleep(random.uniform(0.25, 0.7))

    def dismiss_popups(self) -> None:
        extra = self.layout.get("dismiss") or []
        for point in extra:
            self._tap_norm(float(point[0]), float(point[1]))
        if "close" in self.layout.get("buttons", {}):
            self.tap_rel("close")

    def read_region(self, key: str) -> str:
        image = capture_game_image(self.config, self.adb)
        if image is None:
            return ""
        region = self.layout.get("regions", {}).get(key)
        if not region:
            return ""
        return _ocr(_crop(image, region))

    def search_and_attack(self, kind: str, target: dict[str, int]) -> str:
        dry_run = bool(self.config.get("dry_run", True))
        in_flight = len(self.store.in_flight())
        ok_conc, msg = concurrent_ok(in_flight, self.config)
        if not ok_conc:
            return "wait_return"

        self.dismiss_popups()
        time.sleep(random.uniform(0.3, 0.8))
        self.tap_rel("map")
        time.sleep(random.uniform(0.6, 1.2))
        self.tap_rel("search")
        time.sleep(random.uniform(0.4, 0.9))
        x, y = int(target["x"]), int(target["y"])
        self.tap_rel("coord_x")
        self.adb.text(str(x))
        self.tap_rel("coord_y")
        self.adb.text(str(y))
        self.tap_rel("search_go")
        time.sleep(random.uniform(1.2, 2.0))
        self.tap_rel("target_center")
        time.sleep(random.uniform(0.6, 1.1))
        self.tap_rel("attack")
        time.sleep(random.uniform(0.8, 1.4))
        return self._finish_attack(kind, target)

    def on_screen_attack(self, kind: str) -> str:
        jitter = float((self.config.get("bluestacks") or {}).get("center_jitter") or 0.06)
        center = self.layout["buttons"].get("target_center") or [0.50, 0.46]
        nx = min(0.72, max(0.28, float(center[0]) + random.uniform(-jitter, jitter)))
        ny = min(0.62, max(0.32, float(center[1]) + random.uniform(-jitter, jitter)))
        self.dismiss_popups()
        time.sleep(random.uniform(0.3, 0.7))
        self.tap_rel("map")
        time.sleep(random.uniform(0.5, 1.0))
        self._tap_norm(nx, ny)
        time.sleep(random.uniform(0.6, 1.2))
        self.tap_rel("attack")
        time.sleep(random.uniform(0.8, 1.4))
        fake_target = {
            "kingdom": int((self.config.get("baron_attacks") or {}).get("kingdom", 0)),
            "x": int(nx * 1000),
            "y": int(ny * 1000),
        }
        return self._finish_attack(kind, fake_target)

    def _finish_attack(self, kind: str, target: dict[str, int]) -> str:
        dry_run = bool(self.config.get("dry_run", True))
        kid = int(target.get("kingdom", 0))
        x, y = int(target["x"]), int(target["y"])
        commander_no = parse_commander_number(self.read_region("commander_number"))
        if commander_no is None:
            commander_no = self._next_commander
        ok_cmd, cmd_msg = commander_number_ok(commander_no, self.config)
        if not ok_cmd:
            self.store.live.stopped_reason = cmd_msg
            self.telegram.report_stop(cmd_msg)
            self.tap_rel("close")
            return "stop"
        self._next_commander = int(commander_no) + 1
        one_way = parse_march_seconds(self.read_region("march_time")) or 180
        shot = capture_game_image(self.config, self.adb)
        shot_path = None
        if shot is not None:
            shot_path = save_shot(shot, f"{kind}_{x}_{y}_{int(time.time())}.png")
        if dry_run:
            self.tap_rel("close")
        else:
            self.tap_rel("send")
            time.sleep(random.uniform(0.5, 1.0))
        march = self.store.register_march(
            commander_no,
            commander_no,
            kind,
            kid,
            x,
            y,
            one_way,
            str(shot_path) if shot_path else "",
        )
        self.telegram.report_attack(
            self.store.live.account or "BlueStacks",
            kind,
            kid,
            x,
            y,
            commander_no,
            one_way,
            int(march.return_at - march.sent_at),
            shot_path,
            dry_run=dry_run,
        )
        delay = triangular_delay(self.config.get("attack_delay_seconds") or [4, 10])
        self.store.live.next_attack_at = time.time() + delay
        self.store.save()
        time.sleep(delay)
        return kind

    def run_cycle(self) -> str:
        kind = str(self.config.get("current_target_kind") or "baron")
        style = str(self.config.get("attack_style") or "on_screen")
        if style == "search_coords":
            targets = load_targets()
            rows = targets.get({"baron": "barons", "nomad": "nomads", "shogun": "shogun"}.get(kind, "barons")) or []
            if not rows:
                logger.info("targets.json пуст — для поиска по координатам добавь цели")
                return "no_targets"
            sent = 0
            for target in rows:
                if not concurrent_ok(len(self.store.in_flight()), self.config)[0]:
                    return "wait_return"
                result = self.search_and_attack(kind, target)
                if result == "stop":
                    return "stop"
                sent += 1
            return f"client:{sent}"

        if not concurrent_ok(len(self.store.in_flight()), self.config)[0]:
            return "wait_return"
        result = self.on_screen_attack(kind)
        if result == "stop":
            return "stop"
        return f"client:1"
