from __future__ import annotations

from typing import Any

from e4kbot.worlds import is_world_npc_kind


class KingdomNpcModule:
    """Shared on-screen cycle for other-kingdom towers/forts."""

    is_stub = False

    def __init__(self, spec_id: str, kind: str) -> None:
        self.spec_id = spec_id
        self.kind = kind

    def run_cycle(self, driver: Any | None = None) -> str:
        if driver is None:
            return "idle"
        if not is_world_npc_kind(self.kind):
            return "idle"
        return driver.on_screen_attack(self.kind)


class BarbarianTowersModule(KingdomNpcModule):
    def __init__(self) -> None:
        super().__init__("barbarian_towers", "barbarian_tower")


class DesertTowersModule(KingdomNpcModule):
    def __init__(self) -> None:
        super().__init__("desert_towers", "desert_tower")


class CultistTowersModule(KingdomNpcModule):
    def __init__(self) -> None:
        super().__init__("cultist_towers", "cultist_tower")


class StormFortsModule(KingdomNpcModule):
    def __init__(self) -> None:
        super().__init__("storm_forts", "storm_fort")
