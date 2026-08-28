from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from loguru import logger

from e4kbot.modes.catalog import MODE_BY_ID, ModeSpec, default_campaign_queue, ordered_mode_specs
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


def _normalize_item(mode_id: str, item: dict[str, Any] | None = None) -> dict[str, Any]:
    spec = MODE_BY_ID[mode_id]
    prev = item or {}
    return {
        "mode": mode_id,
        "count": max(0, int(prev.get("count") or spec.default_quota)),
        "enabled": bool(prev.get("enabled", False)),
    }


def campaign_queue(config: dict[str, Any]) -> list[dict[str, Any]]:
    """Priority order 1–4. Toggle flags come from config; disabled modes stay in the list off."""
    campaign = config.get("campaign") or {}
    raw = campaign.get("queue")
    if not isinstance(raw, list) or not raw:
        raw = default_campaign_queue()
    existing: dict[str, dict[str, Any]] = {}
    for item in raw:
        if not isinstance(item, dict):
            continue
        mode_id = str(item.get("mode") or "")
        if mode_id in MODE_BY_ID and mode_id not in existing:
            existing[mode_id] = item
    return [_normalize_item(spec.id, existing.get(spec.id)) for spec in ordered_mode_specs()]


def enabled_mode_ids(config: dict[str, Any]) -> list[str]:
    return [str(item["mode"]) for item in campaign_queue(config) if item.get("enabled")]


def apply_campaign_queue(
    config: dict[str, Any],
    queue: list[dict[str, Any]] | None = None,
    enabled_modes: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Persist toggles for every catalog mode and keep legacy modes.* in sync."""
    existing = {str(item.get("mode")): item for item in campaign_queue(config)}
    incoming: dict[str, dict[str, Any]] = {}
    for item in queue or []:
        if not isinstance(item, dict):
            continue
        mode_id = str(item.get("mode") or "")
        if mode_id in MODE_BY_ID:
            incoming[mode_id] = item
    flags = enabled_modes if isinstance(enabled_modes, dict) else {}
    merged: list[dict[str, Any]] = []
    for spec in ordered_mode_specs():
        prev = incoming.get(spec.id) or existing.get(spec.id) or {}
        enabled = bool(prev.get("enabled", False))
        if spec.id in flags:
            enabled = bool(flags[spec.id])
        merged.append(
            {
                "mode": spec.id,
                "count": max(0, int(prev.get("count") or spec.default_quota)),
                "enabled": enabled,
            }
        )
    campaign = dict(config.get("campaign") or {})
    campaign["enabled"] = True
    campaign["queue"] = merged
    config["campaign"] = campaign
    sync_legacy_modes(config)
    first = next((item for item in merged if item.get("enabled")), None)
    if first is not None:
        spec = MODE_BY_ID.get(str(first["mode"]))
        if spec is not None:
            config["current_target_kind"] = spec.target_kind
    return merged


def sync_legacy_modes(config: dict[str, Any]) -> None:
    enabled = set(enabled_mode_ids(config))
    modes = dict(config.get("modes") or {})
    modes["barons"] = "robber_barons" in enabled
    modes["nomads"] = "nomad_camps" in enabled
    modes["shogun"] = "samurai_camps" in enabled
    config["modes"] = modes


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
        if not enabled:
            remaining = 0
        elif mode_id in skipped:
            remaining = 0
        else:
            remaining = max(0, count - sent)
        out.append(
            CampaignStep(
                mode_id=mode_id,
                count=count,
                enabled=enabled,
                spec=spec,
                sent=sent,
                remaining=remaining,
            )
        )
    return out


def _next_unfinished(all_steps: list[CampaignStep], pinned: str = "") -> CampaignStep | None:
    if pinned:
        for step in all_steps:
            if (
                step.mode_id == pinned
                and step.enabled
                and not step.done
                and step.spec.status == "live"
            ):
                return step
    for step in all_steps:
        if step.enabled and not step.done and step.spec.status == "live":
            return step
    return None


def _reset_live_tour(store: StateStore, live_ids: list[str]) -> None:
    sent = dict(store.live.session_by_mode or {})
    for mode_id in live_ids:
        sent[mode_id] = 0
    skipped = [item for item in (store.live.skipped_modes or []) if item not in live_ids]
    store.live.session_by_mode = sent
    store.live.skipped_modes = skipped
    store.live.pinned_mode = ""
    store.save()


def pick_next_step(config: dict[str, Any], store: StateStore) -> CampaignStep | None:
    """Next unfinished ENABLED step. After a full tour, loop — never halt on quota."""
    all_steps = steps(config, store)
    pinned = str(getattr(store.live, "pinned_mode", "") or "")
    found = _next_unfinished(all_steps, pinned)
    if found is not None:
        if pinned and found.mode_id != pinned:
            store.live.pinned_mode = ""
        return found
    if pinned:
        store.live.pinned_mode = ""
    live_ids = [
        step.mode_id
        for step in all_steps
        if step.enabled and step.spec.status == "live"
    ]
    if not live_ids:
        return None
    logger.info(
        "Тур включённых задач закончен — начинаю круг заново, квота не останавливает бота"
    )
    _reset_live_tour(store, live_ids)
    return _next_unfinished(steps(config, store), "")


def snapshot(config: dict[str, Any], store: StateStore) -> dict[str, Any]:
    all_steps = steps(config, store)
    pinned = str(getattr(store.live, "pinned_mode", "") or "")
    current = _next_unfinished(all_steps, pinned)
    if current is None:
        live = [step for step in all_steps if step.enabled and step.spec.status == "live"]
        current = live[0] if live else None
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
                "campaign_priority": int(step.spec.campaign_priority or 0),
            }
            for step in steps(config, store)
        ],
    }
