from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from loguru import logger

from e4kbot.config import save_config
from e4kbot.control import CONTROL, apply_public_settings, public_settings
from e4kbot.modes.catalog import catalog_grouped, catalog_payload
from e4kbot.paths import SHOTS_DIR, WEBUI_DIST
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
        return payload

    @app.get("/api/state")
    def api_state() -> dict[str, Any]:
        return _state_payload()

    @app.get("/api/catalog")
    def api_catalog() -> dict[str, Any]:
        return {"catalog": catalog_payload(), "worlds": catalog_grouped()}

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
