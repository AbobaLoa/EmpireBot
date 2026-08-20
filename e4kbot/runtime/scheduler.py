from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from e4kbot.modes.catalog import MODE_BY_ID, ModeSpec, default_campaign_queue
from e4kbot.state import StateStore


@dataclass(frozen=True)
class CampaignStep:
    mode_id: str
    count: int
    enabled: bool
    spec: ModeSpec
    sent: int
    remaining: int

    @property
    def done(self) -> bool:
        return self.remaining <= 0


def campaign_queue(config: dict[str, Any]) -> list[dict[str, Any]]:
    campaign = config.get("campaign") or {}
    raw = campaign.get("queue")
    if not isinstance(raw, list) or not raw:
        return default_campaign_queue()
    return raw


def steps(config: dict[str, Any], store: StateStore) -> list[CampaignStep]:
    sent_map = dict(store.live.session_by_mode or {})
    skipped = set(store.live.skipped_modes or [])
    out: list[CampaignStep] = []
    for item in campaign_queue(config):
        mode_id = str(item.get("mode") or "")
        spec = MODE_BY_ID.get(mode_id)
        if spec is None:
            continue
        count = max(0, int(item.get("count") or spec.default_quota))
        enabled = bool(item.get("enabled", spec.status == "live"))
        sent = int(sent_map.get(mode_id) or 0)
        if mode_id in skipped:
            sent = max(sent, count)
        out.append(
            CampaignStep(
                mode_id=mode_id,
                count=count,
                enabled=enabled,
                spec=spec,
                sent=sent,
                remaining=max(0, count - sent) if enabled else 0,
            )
        )
    return out


def pick_next_step(config: dict[str, Any], store: StateStore) -> CampaignStep | None:
    """Next unfinished enabled step. Previous marches may still be in-flight."""
    for step in steps(config, store):
        if not step.enabled or step.done:
            continue
        return step
    return None


def snapshot(config: dict[str, Any], store: StateStore) -> dict[str, Any]:
    current = pick_next_step(config, store)
    return {
        "fill_without_waiting_returns": bool(
            (config.get("campaign") or {}).get("fill_without_waiting_returns", True)
        ),
        "current_mode": None if current is None else current.mode_id,
        "steps": [
            {
                "mode": step.mode_id,
                "title_ru": step.spec.title_ru,
                "official_name": step.spec.official_name,
                "kingdom_ru": step.spec.kingdom_ru,
                "status": step.spec.status,
                "enabled": step.enabled,
                "count": step.count,
                "sent": step.sent,
                "remaining": step.remaining,
            }
            for step in steps(config, store)
        ],
    }
