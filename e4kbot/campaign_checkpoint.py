from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from loguru import logger

from e4kbot.paths import DATA_DIR

RESUME_WINDOW_SEC = 10 * 60
CHECKPOINT_PATH = DATA_DIR / "campaign_checkpoint.json"
TOUR_MODE_IDS = (
    "robber_barons",
    "barbarian_towers",
    "desert_towers",
    "cultist_towers",
    "storm_forts",
)


def checkpoint_path() -> Path:
    return CHECKPOINT_PATH


def load_checkpoint(path: Path | None = None) -> dict[str, Any] | None:
    target = path or CHECKPOINT_PATH
    if not target.exists():
        return None
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except Exception:
        logger.warning("Чекпоинт кампании не прочитался — начну с Великой империи")
        return None
    return raw if isinstance(raw, dict) else None


def age_seconds(payload: dict[str, Any] | None, now: float | None = None) -> float | None:
    if not payload:
        return None
    paused_at = float(payload.get("paused_at") or 0)
    progress_at = float(payload.get("last_progress_at") or 0)
    stamp = max(paused_at, progress_at)
    if stamp <= 0:
        return None
    return max(0.0, float(now or time.time()) - stamp)


def should_resume(payload: dict[str, Any] | None, now: float | None = None) -> bool:
    age = age_seconds(payload, now)
    return age is not None and age <= RESUME_WINDOW_SEC


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone().isoformat(timespec="seconds")


def build_checkpoint(
    store: Any,
    client: Any | None = None,
    *,
    now: float | None = None,
    pause: bool = False,
    path: Path | None = None,
) -> dict[str, Any]:
    live = store.live
    stamp = float(now or time.time())
    previous = load_checkpoint(path)
    paused_at = stamp if pause else float((previous or {}).get("paused_at") or 0)
    mode_id = str(live.active_mode or "")
    waiting = str(live.mode or "") == "wait_commanders" or float(live.next_attack_at or 0) > stamp
    assembling = False
    switched = getattr(client, "_switched_world_id", None) if client is not None else None
    need_ge_home = bool(getattr(client, "_need_ge_home", False)) if client is not None else False
    if client is not None:
        try:
            assembling = bool(client._plan_or_picker_open())
        except Exception:
            assembling = False
        if not switched:
            switched = getattr(client, "_switched_world_id", None)
    sent_map = dict(live.session_by_mode or {})
    return {
        "paused_at": paused_at,
        "paused_at_iso": _iso(paused_at) if paused_at else "",
        "last_progress_at": stamp,
        "last_progress_iso": _iso(stamp),
        "mode_id": mode_id,
        "next_step": mode_id,
        "world_id": str(switched or ""),
        "current_world": str(live.current_world or ""),
        "target_kind": str(live.active_mode or ""),
        "session_by_mode": sent_map,
        "skipped_modes": list(live.skipped_modes or []),
        "sent_in_mode": int(sent_map.get(mode_id) or 0),
        "next_attack_at": float(live.next_attack_at or 0),
        "waiting_commanders": waiting,
        "assembling_army": assembling,
        "need_ge_home": need_ge_home,
        "pinned_mode": str(getattr(live, "pinned_mode", None) or mode_id),
        "last_action": str(live.last_action or ""),
        "switched_world_id": str(switched or ""),
        "live_mode": str(live.mode or ""),
        "post_attack_home_pending": bool(getattr(live, "post_attack_home_pending", False)),
        "resume_window_sec": RESUME_WINDOW_SEC,
    }


def write_checkpoint(
    store: Any,
    client: Any | None = None,
    *,
    path: Path | None = None,
    now: float | None = None,
    pause: bool = False,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = build_checkpoint(store, client, now=now, pause=pause, path=path)
    if extra:
        payload.update(extra)
    target = path or CHECKPOINT_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def mark_paused(store: Any, client: Any | None = None, *, path: Path | None = None) -> dict[str, Any]:
    payload = write_checkpoint(store, client, path=path, pause=True)
    logger.info(
        "Чекпоинт паузы {} · мир {} · режим {} · отправлено {} · жду военачальника={} · набор={}",
        payload.get("paused_at_iso"),
        payload.get("current_world") or payload.get("world_id") or "—",
        payload.get("mode_id") or "—",
        payload.get("sent_in_mode"),
        payload.get("waiting_commanders"),
        payload.get("assembling_army"),
    )
    return payload


def restore_into_store(store: Any, payload: dict[str, Any]) -> None:
    store.live.session_by_mode = {
        str(key): int(value) for key, value in (payload.get("session_by_mode") or {}).items()
    }
    store.live.skipped_modes = [str(item) for item in (payload.get("skipped_modes") or [])]
    store.live.active_mode = str(payload.get("mode_id") or store.live.active_mode or "")
    store.live.pinned_mode = str(payload.get("mode_id") or payload.get("pinned_mode") or "")
    store.live.current_world = str(payload.get("current_world") or store.live.current_world or "")
    store.live.last_action = str(payload.get("last_action") or store.live.last_action or "")
    store.live.post_attack_home_pending = bool(payload.get("post_attack_home_pending"))
    nxt = float(payload.get("next_attack_at") or 0)
    live_mode = str(payload.get("live_mode") or "")
    if nxt > time.time() or payload.get("waiting_commanders") or live_mode == "wait_commanders":
        if nxt > time.time():
            store.live.next_attack_at = nxt
        store.live.mode = "wait_commanders"
    elif live_mode:
        store.live.mode = live_mode
    store.save()


def restore_into_client(client: Any | None, payload: dict[str, Any] | None) -> None:
    if client is None or not payload:
        return
    world_id = str(payload.get("switched_world_id") or payload.get("world_id") or "")
    client._switched_world_id = world_id or None
    client._need_ge_home = bool(payload.get("need_ge_home")) and not payload.get("assembling_army")


def start_fresh_from_great_empire(
    store: Any,
    client: Any | None = None,
    *,
    path: Path | None = None,
    config: dict[str, Any] | None = None,
) -> None:
    store.reset_session_stats()
    store.live.mode = "attack"
    store.live.next_attack_at = 0
    mode_id = "robber_barons"
    title = "Замки баронов"
    if config:
        from e4kbot.runtime.scheduler import enabled_mode_ids, pick_next_step

        enabled = set(enabled_mode_ids(config))
        if "robber_barons" not in enabled:
            step = pick_next_step(config, store)
            if step is not None:
                mode_id = step.mode_id
                title = step.spec.title_ru
    store.live.active_mode = mode_id
    store.live.pinned_mode = ""
    ge_modes = {
        "robber_barons",
        "nomad_camps",
        "samurai_camps",
        "alien_castles",
        "bloodcrows",
    }
    need_ge = mode_id in ge_modes
    store.live.current_world = "Великая империя" if need_ge else ""
    store.live.last_action = f"старт с приоритета: {title}"
    store.save()
    if client is not None:
        client._switched_world_id = None
        client._need_ge_home = need_ge
        client._hunt_queue = []
        client._blocked_screen_targets = []
    logger.info(
        "Пауза дольше 10 мин или нет чекпоинта — начинаю с включённого приоритета {} ({})",
        title,
        mode_id,
    )
    write_checkpoint(store, client, path=path, pause=False, extra={"need_ge_home": need_ge})


def apply_on_start(
    store: Any,
    client: Any | None = None,
    *,
    path: Path | None = None,
    config: dict[str, Any] | None = None,
) -> str:
    """Process start. Resume ≤10 min checkpoint, else first enabled priority."""
    payload = load_checkpoint(path)
    if should_resume(payload):
        assert payload is not None
        restore_into_store(store, payload)
        restore_into_client(client, payload)
        age = age_seconds(payload) or 0
        logger.info(
            "Возобновляю чекпоинт {:.0f}с назад: {} / {} (набор={}, жду военачальника={})",
            age,
            payload.get("mode_id") or "—",
            payload.get("current_world") or payload.get("world_id") or "—",
            payload.get("assembling_army"),
            payload.get("waiting_commanders"),
        )
        return "resume"
    start_fresh_from_great_empire(store, client, path=path, config=config)
    return "restart"


def apply_on_enable(
    store: Any,
    client: Any | None = None,
    *,
    path: Path | None = None,
    config: dict[str, Any] | None = None,
) -> str:
    """Num1 / panel start after a user pause."""
    payload = load_checkpoint(path)
    if should_resume(payload):
        assert payload is not None
        restore_into_store(store, payload)
        restore_into_client(client, payload)
        age = age_seconds(payload) or 0
        logger.info(
            "Пауза {:.0f}с ≤ 10 мин — продолжаю {} с того же места",
            age,
            payload.get("mode_id") or "кампанию",
        )
        if payload.get("assembling_army"):
            logger.info("Проверяю экран: если набор армии открыт — не начинаю тур заново")
            if client is not None:
                try:
                    if client._plan_or_picker_open():
                        logger.info("Набор армии всё ещё открыт — продолжаю заполнять волны")
                    else:
                        logger.info("Набор армии закрыт — остаюсь в текущем мире, тур не сбрасываю")
                except Exception:
                    logger.info("Экран набора не проверился — продолжаю с чекпоинта")
        if payload.get("waiting_commanders"):
            logger.info("Продолжаю ждать возврат военачальника для добивания квоты")
        return "resume"
    start_fresh_from_great_empire(store, client, path=path, config=config)
    return "restart"
