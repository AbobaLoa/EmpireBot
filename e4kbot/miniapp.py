from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from loguru import logger

from e4kbot.config import save_config
from e4kbot.control import CONTROL, apply_public_settings, public_settings
from e4kbot.farm_reports import FarmLedger
from e4kbot.modes.catalog import catalog_grouped, catalog_payload
from e4kbot.paths import DATA_DIR, SHOTS_DIR, WEBUI_DIST
from e4kbot.runtime.scheduler import snapshot
from e4kbot.state import StateStore


def create_app(store: StateStore, config: dict[str, Any] | None = None) -> FastAPI:
    live_config = config if config is not None else {}
    app = FastAPI(title="EmpireBot", docs_url="/api/docs", redoc_url=None)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://127.0.0.1:5173",
            "http://localhost:5173",
            "http://127.0.0.1:8766",
            "http://localhost:8766",
            "http://tauri.localhost",
            "https://tauri.localhost",
            "tauri://localhost",
        ],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    def _state_payload() -> dict[str, Any]:
        store.prune()
        payload = store.live.to_dict()
        payload.update(CONTROL.snapshot())
        if live_config:
            payload["campaign"] = snapshot(live_config, store)
            payload["catalog"] = catalog_payload()
            payload["worlds"] = catalog_grouped()
            payload["enabled_modes"] = [
                str(item["mode"])
                for item in (payload["campaign"].get("steps") or [])
                if item.get("enabled")
            ]
            payload["dry_run"] = bool(live_config.get("dry_run"))
        campaign = payload.get("campaign") or {}
        steps = campaign.get("steps") or []
        active = next((step for step in steps if step.get("mode") == payload.get("active_mode")), None)
        if active is None:
            active = next((step for step in steps if step.get("enabled")), None)
        if active is not None:
            payload["attacks_world"] = {
                "sent": int(active.get("sent") or 0),
                "quota": int(active.get("count") or 0),
                "remaining": int(active.get("remaining") or 0),
                "mode": str(active.get("mode") or ""),
            }
        else:
            payload["attacks_world"] = {
                "sent": int(payload.get("session_attacks") or 0),
                "quota": 0,
                "remaining": 0,
                "mode": str(payload.get("active_mode") or ""),
            }
        cycle = ((payload.get("action_timings") or {}).get("attack_cycle") or {})
        payload["timing_summary"] = (
            f"последний {cycle.get('last_seconds', 0)}с · средний {cycle.get('average_seconds', 0)}с · n={cycle.get('count', 0)}"
            if cycle
            else "—"
        )
        action = str(payload.get("last_action") or "").lower()
        if payload.get("post_attack_home_pending"):
            phase = "returned_home"
            next_action = "deselect_home → verify_plain_map → search"
        elif "сообщ" in action or "отч" in action:
            phase = "report_check"
            next_action = "обработать отчёты"
        elif "пикер" in action or "волн" in action or "форм" in action:
            phase = "formation"
            next_action = "проверить 100% и отправить"
        elif "дом" in action:
            phase = "deselect_home"
            next_action = "verify_plain_map → search"
        else:
            phase = "search" if payload.get("enabled") and not payload.get("paused") else "paused"
            next_action = "найти следующую NPC-цель" if phase == "search" else "ожидать Num1"
        payload["phase"] = phase
        payload["next_action"] = next_action
        return payload

    def _draft_path() -> Path:
        return DATA_DIR / "player_attack_draft.json"

    worlds_allowed = {
        "great_empire",
        "everwinter",
        "burning_sands",
        "fire_peaks",
        "storm_islands",
    }

    def _validated_player_draft(body: dict[str, Any]) -> dict[str, Any]:
        max_attacks = int((live_config.get("player_attack_placeholder") or {}).get("max_attacks") or 20)
        world = str(body.get("world") or "")
        if world not in worlds_allowed:
            raise HTTPException(422, "Некорректный мир")
        try:
            x, y = int(body.get("x")), int(body.get("y"))
            attacks = int(body.get("attacks"))
            waves = int(body.get("waves"))
            delay = int(body.get("delay_seconds") or 0)
        except (TypeError, ValueError):
            raise HTTPException(422, "X, Y, атаки, волны и задержка должны быть целыми")
        if not (0 <= x <= 999 and 0 <= y <= 999):
            raise HTTPException(422, "Координаты должны быть 0..999")
        if not (1 <= attacks <= max_attacks):
            raise HTTPException(422, f"Количество атак должно быть 1..{max_attacks}")
        if not (1 <= waves <= 6) or not (0 <= delay <= 86400):
            raise HTTPException(422, "Волны 1..6, задержка 0..86400")
        commander = body.get("commander")
        if commander not in (None, "", "auto"):
            try:
                commander = int(commander)
            except (TypeError, ValueError):
                raise HTTPException(422, "Номер военачальника должен быть Auto или целым")
            if not 1 <= commander <= 99:
                raise HTTPException(422, "Номер военачальника должен быть 1..99")
        return {
            "world": world,
            "x": x,
            "y": y,
            "attacks": attacks,
            "commander": commander or "auto",
            "formation": str(body.get("formation") or "default")[:80],
            "waves": waves,
            "flank": str(body.get("flank") or "center"),
            "tools": str(body.get("tools") or "none")[:80],
            "delay_seconds": delay,
            "schedule": str(body.get("schedule") or "")[:80],
            "stop_conditions": list(body.get("stop_conditions") or [])[:8],
            "implemented": False,
            "saved_at": datetime.now().isoformat(),
            "notice": "Запуск атак на игроков пока не реализован",
        }

    @app.get("/api/state")
    def api_state() -> dict[str, Any]:
        return _state_payload()

    @app.get("/api/catalog")
    def api_catalog() -> dict[str, Any]:
        return {"catalog": catalog_payload(), "worlds": catalog_grouped()}

    @app.get("/api/farm-reports")
    def api_farm_reports() -> dict[str, Any]:
        ledger = FarmLedger()
        return {"summary": ledger.summary(), "reports": ledger.rows()}

    @app.get("/api/player-attack-draft")
    def api_player_attack_draft_get() -> dict[str, Any]:
        path = _draft_path()
        if not path.is_file():
            return {"draft": None, "implemented": False}
        return {"draft": json.loads(path.read_text(encoding="utf-8")), "implemented": False}

    @app.post("/api/player-attack-draft")
    async def api_player_attack_draft_save(request: Request) -> dict[str, Any]:
        draft = _validated_player_draft(await request.json())
        path = _draft_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(draft, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(path)
        return {"ok": True, "draft": draft, "implemented": False}

    @app.delete("/api/player-attack-draft")
    def api_player_attack_draft_delete() -> dict[str, Any]:
        _draft_path().unlink(missing_ok=True)
        return {"ok": True, "draft": None, "implemented": False}

    @app.post("/api/control")
    async def api_control(request: Request) -> dict[str, Any]:
        body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
        if "enabled" in body:
            if body["enabled"]:
                CONTROL.enable()
            else:
                CONTROL.disable()
        elif body.get("start"):
            CONTROL.enable()
        elif body.get("pause"):
            CONTROL.disable()
        elif body.get("toggle", True):
            CONTROL.toggle()
        store.live.paused = not CONTROL.is_enabled()
        store.live.mode = "paused" if store.live.paused else store.live.mode
        store.save()
        return CONTROL.snapshot()

    @app.get("/api/settings")
    def api_settings_get() -> dict[str, Any]:
        return public_settings(live_config) if live_config else {}

    @app.post("/api/settings")
    async def api_settings_post(request: Request) -> Any:
        if not live_config:
            return JSONResponse({"ok": False, "error": "config unavailable"}, status_code=400)
        body = await request.json()
        settings = apply_public_settings(live_config, body)
        save_config(live_config)
        store.live.dry_run = bool(live_config.get("dry_run"))
        store.save()
        return settings

    @app.get("/shots/{name:path}")
    def shots(name: str) -> Any:
        path = (SHOTS_DIR / name).resolve()
        if not str(path).startswith(str(SHOTS_DIR.resolve())) or not path.is_file():
            return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
        return FileResponse(path)

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {"ok": True, "stack": "fastapi+react", "ts": datetime.now().isoformat()}

    dist = WEBUI_DIST
    index = dist / "index.html"
    assets = dist / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=str(assets)), name="assets")

    @app.get("/")
    def spa_index() -> Any:
        if index.is_file():
            return FileResponse(index)
        return JSONResponse(
            {
                "ok": False,
                "error": "React UI is not built",
                "hint": "cd webui && npm install && npm run build",
            },
            status_code=503,
        )

    @app.get("/{path:path}")
    def spa_fallback(path: str) -> Any:
        if path.startswith("api/") or path.startswith("shots/"):
            return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
        if index.is_file():
            candidate = dist / path
            if candidate.is_file() and _inside(dist, candidate):
                return FileResponse(candidate)
            return FileResponse(index)
        return JSONResponse({"ok": False, "error": "not found"}, status_code=404)

    return app


def _inside(root: Path, candidate: Path) -> bool:
    try:
        candidate.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def run_miniapp(
    store: StateStore,
    host: str,
    port: int,
    config: dict[str, Any] | None = None,
) -> None:
    import uvicorn

    WEBUI_DIST.mkdir(parents=True, exist_ok=True)
    app = create_app(store, config)
    logger.info("Панель FastAPI+React: http://{}:{}", host, port)
    uvicorn.run(app, host=host, port=port, log_level="info", access_log=False)
