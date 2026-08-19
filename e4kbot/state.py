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
    cooldown_until: float = 0.0
    screenshot: str = ""
    status: str = "marching"
    movement: str = ""
    timer_source: str = "outbound_x2"

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        now = time.time()
        if now >= self.arrive_at and self.status == "marching":
            data["status"] = "returning"
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
    last_confirmed_one_way_sec: int = 0
    cooldowns: dict[str, float] = field(default_factory=dict)
    marches: list[March] = field(default_factory=list)
    history: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        now = time.time()
        in_flight = [m for m in self.marches if m.return_at > now]
        next_cd = max(0, int(self.next_attack_at - now))
        cooldowns = {
            key: {
                "until": until,
                "remaining_sec": max(0, int(until - now)),
            }
            for key, until in self.cooldowns.items()
            if until > now
        }
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
            "last_confirmed_one_way_sec": self.last_confirmed_one_way_sec,
            "target_cooldowns": cooldowns,
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
        self._load_persisted()

    def _load_persisted(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            cooldowns = raw.get("target_cooldowns") or {}
            self.live.cooldowns = {
                str(key): float(value.get("until") if isinstance(value, dict) else value)
                for key, value in cooldowns.items()
            }
            self.live.last_confirmed_one_way_sec = int(
                raw.get("last_confirmed_one_way_sec") or 0
            )
        except Exception:
            self.live.cooldowns = {}

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
        movement: str = "",
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
            cooldown_until=now + one_way + 3 * 60 * 60,
            screenshot=screenshot,
            movement=movement,
        )
        self.live.marches.append(march)
        self.live.last_confirmed_one_way_sec = one_way
        target_key = self.target_key(kind, kingdom, x, y)
        self.live.cooldowns[target_key] = march.cooldown_until
        self.live.last_attack_at = now
        self.live.last_coords = f"K{kingdom} ({x}, {y})"
        if screenshot:
            self.live.last_screenshot = screenshot
        self.prune()
        self.save()
        return march

    @staticmethod
    def target_key(kind: str, kingdom: int, x: int, y: int) -> str:
        return f"{kind}:{int(kingdom)}:{int(x)}:{int(y)}"

    def target_cooldown_until(
        self,
        kind: str,
        kingdom: int,
        x: int,
        y: int,
    ) -> float:
        return float(self.live.cooldowns.get(self.target_key(kind, kingdom, x, y)) or 0)

    def target_available(
        self,
        kind: str,
        kingdom: int,
        x: int,
        y: int,
        now: float | None = None,
    ) -> bool:
        return self.target_cooldown_until(kind, kingdom, x, y) <= (now or time.time())

    def update_return_timer(
        self,
        commander_no: int,
        return_left_sec: int,
        source: str = "screen",
    ) -> March | None:
        now = time.time()
        for march in self.live.marches:
            if march.commander_no != int(commander_no):
                continue
            march.return_at = now + max(0, int(return_left_sec))
            march.status = "returning"
            march.timer_source = source
            self.save()
            return march
        return None
