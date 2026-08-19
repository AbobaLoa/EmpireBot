from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from e4kbot.paths import STATE_PATH
from e4kbot.safety import MAX_COMMANDER_NUMBER, MAX_CONCURRENT_ATTACKS


@dataclass
class March:
    commander_no: int
    lord_id: int
    kind: str
    kingdom: int
    x: int
    y: int
    sent_at: float
    one_way_sec: int
    arrive_at: float
    return_at: float
    screenshot: str = ""
    status: str = "marching"

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        now = time.time()
        data["arrive_left_sec"] = max(0, int(self.arrive_at - now))
        data["return_left_sec"] = max(0, int(self.return_at - now))
        data["cd_left_sec"] = data["return_left_sec"]
        return data


@dataclass
class LiveState:
    running: bool = False
    dry_run: bool = True
    engine: str = "protocol"
    account: str = ""
    mode: str = "idle"
    last_error: str = ""
    next_attack_at: float = 0.0
    last_attack_at: float = 0.0
    last_coords: str = "—"
    last_screenshot: str = ""
    stopped_reason: str = ""
    marches: list[March] = field(default_factory=list)
    history: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        now = time.time()
        in_flight = [m for m in self.marches if m.return_at > now]
        next_cd = max(0, int(self.next_attack_at - now))
        return {
            "running": self.running,
            "dry_run": self.dry_run,
            "engine": self.engine,
            "account": self.account,
            "mode": self.mode,
            "last_error": self.last_error,
            "stopped_reason": self.stopped_reason,
            "last_coords": self.last_coords,
            "last_screenshot": self.last_screenshot,
            "next_attack_cd_sec": next_cd,
            "in_flight": len(in_flight),
            "max_concurrent": MAX_CONCURRENT_ATTACKS,
            "max_commander": MAX_COMMANDER_NUMBER,
            "marches": [m.to_dict() for m in in_flight],
            "history": self.history[-20:],
            "server_time": int(now),
        }


class StateStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or STATE_PATH
        self.live = LiveState()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = self.live.to_dict()
        payload["marches_raw"] = [m.to_dict() for m in self.live.marches]
        self.path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def prune(self) -> None:
        now = time.time()
        keep: list[March] = []
        for march in self.live.marches:
            if march.return_at > now - 30:
                keep.append(march)
            else:
                march.status = "returned"
                self.live.history.append(march.to_dict())
        self.live.marches = keep
        self.live.history = self.live.history[-40:]

    def in_flight(self) -> list[March]:
        now = time.time()
        return [m for m in self.live.marches if m.return_at > now]

    def next_return_at(self) -> float | None:
        active = self.in_flight()
        if not active:
            return None
        return min(m.return_at for m in active)

    def register_march(
        self,
        commander_no: int,
        lord_id: int,
        kind: str,
        kingdom: int,
        x: int,
        y: int,
        one_way_sec: int,
        screenshot: str = "",
    ) -> March:
        now = time.time()
        one_way = max(1, int(one_way_sec))
        march = March(
            commander_no=int(commander_no),
            lord_id=int(lord_id),
            kind=kind,
            kingdom=int(kingdom),
            x=int(x),
            y=int(y),
            sent_at=now,
            one_way_sec=one_way,
            arrive_at=now + one_way,
            return_at=now + one_way * 2,
            screenshot=screenshot,
        )
        self.live.marches.append(march)
        self.live.last_attack_at = now
        self.live.last_coords = f"K{kingdom} ({x}, {y})"
        if screenshot:
            self.live.last_screenshot = screenshot
        self.prune()
        self.save()
        return march
