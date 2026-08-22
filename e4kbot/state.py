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
    next_send_at: float = 0.0
    last_attack_at: float = 0.0
    last_coords: str = "—"
    last_screenshot: str = ""
    stopped_reason: str = ""
    last_confirmed_one_way_sec: int = 0
    paused: bool = False
    cooldowns: dict[str, float] = field(default_factory=dict)
    return_speed_pct: int = 0
    marches: list[March] = field(default_factory=list)
    history: list[dict[str, Any]] = field(default_factory=list)
    session_attacks: int = 0
    session_gold: int = 0
    session_rubies: int = 0
    session_by_mode: dict[str, int] = field(default_factory=dict)
    skipped_modes: list[str] = field(default_factory=list)
    active_mode: str = ""
    target_hits: dict[str, int] = field(default_factory=dict)
    samurai_remaining: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from e4kbot.control import CONTROL

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
        paused = not CONTROL.is_enabled()
        return {
            "running": self.running,
            "paused": paused,
            "enabled": not paused,
            "hotkey": CONTROL.hotkey,
            "dry_run": self.dry_run,
            "engine": self.engine,
            "account": self.account,
            "mode": self.mode,
            "last_error": self.last_error,
            "stopped_reason": self.stopped_reason,
            "last_coords": self.last_coords,
            "last_screenshot": self.last_screenshot,
            "last_confirmed_one_way_sec": self.last_confirmed_one_way_sec,
            "return_speed_pct": self.return_speed_pct,
            "target_cooldowns": cooldowns,
            "next_attack_cd_sec": max(
                next_cd, max(0, int(self.next_send_at - now))
            ),
            "in_flight": len(in_flight),
            "max_concurrent": MAX_CONCURRENT_ATTACKS,
            "max_commander": MAX_COMMANDER_NUMBER,
            "marches": [m.to_dict() for m in in_flight],
            "history": self.history[-20:],
            "session_attacks": int(self.session_attacks),
            "session_gold": int(self.session_gold),
            "session_rubies": int(self.session_rubies),
            "session_by_mode": dict(self.session_by_mode),
            "skipped_modes": list(self.skipped_modes),
            "active_mode": self.active_mode,
            "target_hits": dict(self.target_hits),
            "samurai_remaining": dict(self.samurai_remaining),
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
            self.live.last_coords = str(raw.get("last_coords") or "—")
            self.live.last_screenshot = str(raw.get("last_screenshot") or "")
            hits = raw.get("target_hits") or {}
            self.live.target_hits = {
                str(key): int(value) for key, value in hits.items() if int(value) > 0
            }
            remaining = raw.get("samurai_remaining") or {}
            self.live.samurai_remaining = {
                str(key): int(value) for key, value in remaining.items()
            }
            self.live.session_attacks = int(raw.get("session_attacks") or 0)
            self.live.session_gold = int(raw.get("session_gold") or 0)
            self.live.session_rubies = int(raw.get("session_rubies") or 0)
            self.live.session_by_mode = {
                str(key): int(value)
                for key, value in (raw.get("session_by_mode") or {}).items()
            }
            self.live.skipped_modes = [
                str(value) for value in (raw.get("skipped_modes") or [])
            ]
            self.live.active_mode = str(raw.get("active_mode") or "")
            self.live.history = list(raw.get("history") or [])
            march_fields = set(March.__dataclass_fields__)
            self.live.marches = [
                March(**{key: value for key, value in item.items() if key in march_fields})
                for item in (raw.get("marches_raw") or [])
                if isinstance(item, dict)
            ]
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
        hit_key = f"{kind}:{int(x)}:{int(y)}"
        hits = int(self.live.target_hits.get(hit_key) or 0) + 1
        self.live.target_hits[hit_key] = hits
        if kind == "samurai":
            budget = self.live.samurai_remaining.get(hit_key)
            if budget is None:
                budget = 10
            left = max(0, int(budget) - 1)
            self.live.samurai_remaining[hit_key] = left
            cooldown_until = now + 24 * 60 * 60 if left <= 0 else 0.0
        else:
            cooldown_until = now + one_way + 3 * 60 * 60
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
            cooldown_until=cooldown_until,
            screenshot=screenshot,
            movement=movement,
            timer_source="outbound_x2",
        )
        self.live.marches.append(march)
        self.live.session_attacks += 1
        mode_id = self.live.active_mode or kind
        self.live.session_by_mode[mode_id] = int(self.live.session_by_mode.get(mode_id) or 0) + 1
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

    def target_hits(self, kind: str, coords: tuple[int, int] | None) -> int:
        if not coords:
            return 0
        return int(self.live.target_hits.get(f"{kind}:{int(coords[0])}:{int(coords[1])}") or 0)

    def samurai_remaining_for(self, coords: tuple[int, int] | None) -> int | None:
        if not coords:
            return None
        key = f"samurai:{int(coords[0])}:{int(coords[1])}"
        if key not in self.live.samurai_remaining:
            return None
        return int(self.live.samurai_remaining[key])

    def set_samurai_remaining(self, coords: tuple[int, int] | None, remaining: int) -> None:
        if not coords:
            return
        key = f"samurai:{int(coords[0])}:{int(coords[1])}"
        self.live.samurai_remaining[key] = max(0, int(remaining))
        self.save()

    def camp_has_samurai_budget(self, coords: tuple[int, int] | None) -> bool:
        remaining = self.samurai_remaining_for(coords)
        if remaining is not None:
            return remaining > 0
        return self.target_hits("samurai", coords) < 10

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
        if kind == "samurai":
            return self.camp_has_samurai_budget((x, y))
        return self.target_cooldown_until(kind, kingdom, x, y) <= (now or time.time())

    def record_loot(self, gold: int = 0, rubies: int = 0) -> None:
        self.live.session_gold += max(0, int(gold or 0))
        self.live.session_rubies += max(0, int(rubies or 0))

    def session_summary(self) -> dict[str, int]:
        return {
            "attacks": int(self.live.session_attacks),
            "gold": int(self.live.session_gold),
            "rubies": int(self.live.session_rubies),
        }

    def reset_session_stats(self) -> None:
        self.live.session_attacks = 0
        self.live.session_gold = 0
        self.live.session_rubies = 0
        self.live.session_by_mode = {}
        self.live.skipped_modes = []
        self.live.active_mode = ""

    def skip_mode(self, mode_id: str) -> None:
        if mode_id not in self.live.skipped_modes:
            self.live.skipped_modes.append(mode_id)
            self.save()

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
