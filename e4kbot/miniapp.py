from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, send_from_directory
from loguru import logger

from e4kbot.paths import SHOTS_DIR, WEBAPP_DIR
from e4kbot.state import StateStore


def create_app(store: StateStore) -> Flask:
    app = Flask(__name__, static_folder=str(WEBAPP_DIR), static_url_path="")

    @app.get("/")
    def index() -> Any:
        return send_from_directory(WEBAPP_DIR, "index.html")

    @app.get("/api/state")
    def api_state() -> Any:
        store.prune()
        return jsonify(store.live.to_dict())

    @app.get("/shots/<path:name>")
    def shots(name: str) -> Any:
        return send_from_directory(SHOTS_DIR, name)

    @app.get("/health")
    def health() -> Any:
        return jsonify({"ok": True, "ts": datetime.now().isoformat()})

    return app


def run_miniapp(store: StateStore, host: str, port: int) -> None:
    WEBAPP_DIR.mkdir(parents=True, exist_ok=True)
    app = create_app(store)
    logger.info(f"Mini App: http://{host}:{port}")
    app.run(host=host, port=port, debug=False, use_reloader=False, threaded=True)
