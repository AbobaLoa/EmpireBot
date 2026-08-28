from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from e4kbot.paths import STATE_PATH
from e4kbot.safety import MAX_COMMANDER_NUMBER, MAX_CONCURRENT_ATTACKS

NOMAD_HITS_PER_CAMP = 11


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
    pinned_mode: str = ""
    target_hits: dict[str, int] = field(default_factory=dict)
    samurai_remaining: dict[str, int] = field(default_factory=dict)
    nomad_remaining: dict[str, int] = field(default_factory=dict)
    nomad_farm_coords: list[int] | None = None
    nomad_farm_point: list[float] | None = None
    last_action: str = ""
    unopened_worlds: list[str] = field(default_factory=list)
    attack_report: str = ""
    current_world: str = ""
    reports_processed: int = 0
    farm_summary: dict[str, Any] = field(default_factory=dict)
    action_timings: dict[str, dict[str, float]] = field(default_factory=dict)
    post_attack_home_pending: bool = False
    central_castles: dict[str, dict[str, Any]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from e4kbot.control import CONTROL, hotkey_label

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
        if paused:
            status_label = "пауза"
        elif self.running:
            status_label = "работает"
        else:
            status_label = "ожидание"
        return {
            "running": self.running,
            "paused": paused,
            "enabled": not paused,
            "status_label": status_label,
            "last_action": self.last_action or self.last_coords or "—",
            "unopened_worlds": list(self.unopened_worlds),
            "attack_report": self.attack_report,
            "current_world": self.current_world,
            "reports_processed": int(self.reports_processed),
            "farm_summary": dict(self.farm_summary),
            "action_timings": dict(self.action_timings),
            "post_attack_home_pending": bool(self.post_attack_home_pending),
            "central_castles": dict(self.central_castles),
            "hotkey": CONTROL.hotkey,
            "hotkey_label": hotkey_label(CONTROL.hotkey),
            "start_hotkey": CONTROL.start_hotkey,
            "start_hotkey_label": hotkey_label(CONTROL.start_hotkey),
            "pause_hotkey": CONTROL.hotkey,
            "pause_hotkey_label": hotkey_label(CONTROL.hotkey),
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
            "pinned_mode": self.pinned_mode,
            "target_hits": dict(self.target_hits),
            "samurai_remaining": dict(self.samurai_remaining),
            "nomad_remaining": dict(self.nomad_remaining),
            "nomad_farm_coords": list(self.nomad_farm_coords) if self.nomad_farm_coords else None,
            "nomad_farm_point": list(self.nomad_farm_point) if self.nomad_farm_point else None,
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
            nomad_remaining = raw.get("nomad_remaining") or {}
            self.live.nomad_remaining = {
                str(key): int(value) for key, value in nomad_remaining.items()
            }
            farm = raw.get("nomad_farm_coords")
            if isinstance(farm, (list, tuple)) and len(farm) == 2:
                self.live.nomad_farm_coords = [int(farm[0]), int(farm[1])]
            point = raw.get("nomad_farm_point")
            if isinstance(point, (list, tuple)) and len(point) == 2:
                self.live.nomad_farm_point = [float(point[0]), float(point[1])]
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
            self.live.pinned_mode = str(raw.get("pinned_mode") or "")
            self.live.unopened_worlds = [
                str(value) for value in (raw.get("unopened_worlds") or [])
            ]
            self.live.attack_report = str(raw.get("attack_report") or "")
            self.live.current_world = str(raw.get("current_world") or "")
            self.live.reports_processed = int(raw.get("reports_processed") or 0)
            self.live.farm_summary = dict(raw.get("farm_summary") or {})
            self.live.action_timings = dict(raw.get("action_timings") or {})
            self.live.post_attack_home_pending = bool(raw.get("post_attack_home_pending"))
            self.live.central_castles = dict(raw.get("central_castles") or {})
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
        if kind == "nomad":
            snapped = self.canonicalize_nomad_coords((int(x), int(y)))
            if snapped:
                x, y = snapped
        hit_key = f"{kind}:{int(x)}:{int(y)}"
        hits = int(self.live.target_hits.get(hit_key) or 0) + 1
        self.live.target_hits[hit_key] = hits
        if kind in {"samurai", "nomad"}:
            bucket = self.live.samurai_remaining if kind == "samurai" else self.live.nomad_remaining
            budget = bucket.get(hit_key)
            if budget is None:
                budget = NOMAD_HITS_PER_CAMP if kind == "nomad" else 10
            left = max(0, int(budget) - 1)
            bucket[hit_key] = left
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
        self.live.post_attack_home_pending = True
        self.live.last_coords = f"K{kingdom} ({x}, {y})"
        if screenshot:
            self.live.last_screenshot = screenshot
        if kind == "nomad":
            if self.camp_has_nomad_budget((int(x), int(y))):
                self.live.nomad_farm_coords = [int(x), int(y)]
            else:
                farm = self.nomad_farm_xy()
                if farm and abs(farm[0] - int(x)) <= 4 and abs(farm[1] - int(y)) <= 4:
                    self.live.nomad_farm_coords = None
                    self.live.nomad_farm_point = None
        self.prune()
        self.save()
        return march

    def canonicalize_nomad_coords(
        self, coords: tuple[int, int] | None
    ) -> tuple[int, int] | None:
        """Treat ±4 map jitter as the same nomad camp. Prefer the farm, then closest."""
        if not coords:
            return None
        x0, y0 = int(coords[0]), int(coords[1])
        farm = self.nomad_farm_xy()
        if farm and abs(farm[0] - x0) <= 4 and abs(farm[1] - y0) <= 4:
            return farm
        ranked: list[tuple[int, int, int, int]] = []
        for key, hits in (self.live.target_hits or {}).items():
            parsed = self._parse_nomad_key(key)
            if parsed is None or int(hits) <= 0:
                continue
            x, y = parsed
            if abs(x - x0) <= 4 and abs(y - y0) <= 4:
                ranked.append((abs(x - x0) + abs(y - y0), -int(hits), x, y))
        if ranked:
            ranked.sort()
            return (ranked[0][2], ranked[0][3])
        for key in self.live.nomad_remaining or {}:
            parsed = self._parse_nomad_key(key)
            if parsed is None:
                continue
            x, y = parsed
            if abs(x - x0) <= 4 and abs(y - y0) <= 4:
                return (x, y)
        return (x0, y0)

    @staticmethod
    def _parse_nomad_key(key: str) -> tuple[int, int] | None:
        parts = str(key).split(":")
        if len(parts) != 3 or parts[0] != "nomad":
            return None
        try:
            return (int(parts[1]), int(parts[2]))
        except ValueError:
            return None

    def nomad_farm_xy(self) -> tuple[int, int] | None:
        farm = self.live.nomad_farm_coords
        if not isinstance(farm, (list, tuple)) or len(farm) != 2:
            return None
        return (int(farm[0]), int(farm[1]))

    def nomad_farm_screen(self) -> tuple[float, float] | None:
        point = self.live.nomad_farm_point
        if not isinstance(point, (list, tuple)) or len(point) != 2:
            return None
        return (float(point[0]), float(point[1]))

    def set_nomad_farm(
        self,
        coords: tuple[int, int] | None,
        point: tuple[float, float] | None = None,
    ) -> None:
        if not coords:
            return
        snapped = self.canonicalize_nomad_coords(coords) or (int(coords[0]), int(coords[1]))
        self.live.nomad_farm_coords = [int(snapped[0]), int(snapped[1])]
        if (
            point is not None
            and not (abs(float(point[0]) - 0.50) < 0.02 and abs(float(point[1]) - 0.50) < 0.02)
        ):
            self.live.nomad_farm_point = [float(point[0]), float(point[1])]
        self.save()

    def clear_nomad_farm(self) -> None:
        self.live.nomad_farm_coords = None
        self.live.nomad_farm_point = None
        self.save()

    def target_hits(self, kind: str, coords: tuple[int, int] | None) -> int:
        if not coords:
            return 0
        if kind == "nomad":
            coords = self.canonicalize_nomad_coords(coords)
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

    def nomad_remaining_for(self, coords: tuple[int, int] | None) -> int | None:
        coords = self.canonicalize_nomad_coords(coords)
        if not coords:
            return None
        key = f"nomad:{int(coords[0])}:{int(coords[1])}"
        if key not in self.live.nomad_remaining:
            return None
        return int(self.live.nomad_remaining[key])

    def set_nomad_remaining(self, coords: tuple[int, int] | None, remaining: int) -> None:
        coords = self.canonicalize_nomad_coords(coords)
        if not coords:
            return
        key = f"nomad:{int(coords[0])}:{int(coords[1])}"
        self.live.nomad_remaining[key] = max(0, int(remaining))
        self.save()

    def camp_has_nomad_budget(self, coords: tuple[int, int] | None) -> bool:
        if self.target_hits("nomad", coords) >= NOMAD_HITS_PER_CAMP:
            return False
        remaining = self.nomad_remaining_for(coords)
        if remaining is not None:
            return remaining > 0
        return True

    def apply_nomad_ocr_remaining(self, coords: tuple[int, int] | None, remaining_from_ocr: int) -> int:
        """Never raise remaining after hits started, never go past 11 - hits."""
        if not coords:
            return 0
        hits = self.target_hits("nomad", coords)
        cap = max(0, NOMAD_HITS_PER_CAMP - hits)
        ocr = max(0, min(int(remaining_from_ocr), NOMAD_HITS_PER_CAMP))
        current = self.nomad_remaining_for(coords)
        if hits >= NOMAD_HITS_PER_CAMP:
            chosen = 0
        elif hits == 0 or current is None:
            chosen = min(ocr, cap)
        else:
            chosen = min(int(current), ocr, cap)
        self.set_nomad_remaining(coords, chosen)
        return chosen

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
        if kind == "nomad":
            return self.camp_has_nomad_budget((x, y))
        return self.target_cooldown_until(kind, kingdom, x, y) <= (now or time.time())

    def record_loot(self, gold: int = 0, rubies: int = 0) -> None:
        self.live.session_gold += max(0, int(gold or 0))
        self.live.session_rubies += max(0, int(rubies or 0))

    def record_timing(self, action: str, seconds: float) -> None:
        row = dict(self.live.action_timings.get(action) or {})
        count = int(row.get("count") or 0) + 1
        total = float(row.get("total_seconds") or 0.0) + max(0.0, float(seconds))
        self.live.action_timings[action] = {
            "count": count,
            "total_seconds": round(total, 3),
            "average_seconds": round(total / count, 3),
            "last_seconds": round(max(0.0, float(seconds)), 3),
        }

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
        self.live.pinned_mode = ""

    def skip_mode(self, mode_id: str) -> None:
        if mode_id not in self.live.skipped_modes:
            self.live.skipped_modes.append(mode_id)
        if self.live.pinned_mode == mode_id:
            self.live.pinned_mode = ""
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
