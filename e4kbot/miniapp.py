from __future__ import annotations

from datetime import datetime
from typing import Any

from flask import Flask, jsonify, request, send_from_directory
from loguru import logger

from e4kbot.config import save_config
from e4kbot.control import CONTROL, apply_public_settings, public_settings
from e4kbot.paths import SHOTS_DIR, WEBAPP_DIR
from e4kbot.state import StateStore


def create_app(store: StateStore, config: dict[str, Any] | None = None) -> Flask:
    app = Flask(__name__, static_folder=str(WEBAPP_DIR), static_url_path="")
    live_config = config if config is not None else {}

    @app.get("/")
    def index() -> Any:
        return send_from_directory(WEBAPP_DIR, "index.html")

    @app.get("/api/state")
    def api_state() -> Any:
        store.prune()
        payload = store.live.to_dict()
        payload.update(CONTROL.snapshot())
        return jsonify(payload)

    @app.post("/api/control")
    def api_control() -> Any:
        body = request.get_json(silent=True) or {}
        if "enabled" in body:
            if body["enabled"]:
                CONTROL.enable()
            else:
                CONTROL.disable()
        elif body.get("toggle", True):
            CONTROL.toggle()
        store.live.paused = not CONTROL.is_enabled()
        store.live.mode = "paused" if store.live.paused else store.live.mode
        store.save()
        return jsonify(CONTROL.snapshot())

    @app.get("/api/settings")
    def api_settings_get() -> Any:
        return jsonify(public_settings(live_config) if live_config else {})

    @app.post("/api/settings")
    def api_settings_post() -> Any:
        body = request.get_json(silent=True) or {}
        if not live_config:
            return jsonify({"ok": False, "error": "config unavailable"}), 400
        settings = apply_public_settings(live_config, body)
        save_config(live_config)
        store.live.dry_run = bool(live_config.get("dry_run"))
        store.save()
        return jsonify(settings)

    @app.get("/shots/<path:name>")
    def shots(name: str) -> Any:
        return send_from_directory(SHOTS_DIR, name)

    @app.get("/health")
    def health() -> Any:
        return jsonify({"ok": True, "ts": datetime.now().isoformat()})

    return app


def run_miniapp(
    store: StateStore,
    host: str,
    port: int,
    config: dict[str, Any] | None = None,
) -> None:
    WEBAPP_DIR.mkdir(parents=True, exist_ok=True)
    app = create_app(store, config)
    logger.info(f"Mini App: http://{host}:{port}")
    app.run(host=host, port=port, debug=False, use_reloader=False, threaded=True)
