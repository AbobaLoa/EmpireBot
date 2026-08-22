from __future__ import annotations

from e4kbot.attacks.base import AttackModule, StubAttackModule
from e4kbot.attacks.robber_barons import RobberBaronsModule
from e4kbot.attacks.samurai_camps import SamuraiCampsModule
from e4kbot.modes.catalog import MODES

_LIVE = {
    "robber_barons": RobberBaronsModule,
    "samurai_camps": SamuraiCampsModule,
}

ATTACK_MODULES: dict[str, AttackModule] = {}
for spec in MODES:
    factory = _LIVE.get(spec.id)
    ATTACK_MODULES[spec.id] = factory() if factory else StubAttackModule(spec.id)


def get_attack_module(mode_id: str) -> AttackModule:
    return ATTACK_MODULES.get(mode_id) or StubAttackModule(mode_id or "unknown")
