from __future__ import annotations

from typing import Any


class RobberBaronsModule:
    """Robber baron castles: nearest to the main castle, one center wave, no tools."""

    spec_id = "robber_barons"
    is_stub = False

    def run_cycle(self, driver: Any | None = None) -> str:
        if driver is None:
            return "idle"
        return driver.on_screen_attack("baron")
