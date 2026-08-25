from __future__ import annotations

from typing import Any, Protocol


class AttackModule(Protocol):
    """One attack kind. Shared I/O stays on the BlueStacks driver."""

    spec_id: str

    def run_cycle(self, driver: Any | None = None) -> str:
        """Hunt / prepare / send one cycle. Stubs must not click."""


class StubAttackModule:
    """Placeholder service for a catalog mode that is not implemented yet."""

    is_stub = True

    def __init__(self, spec_id: str) -> None:
        self.spec_id = spec_id

    def run_cycle(self, driver: Any | None = None) -> str:
        return f"stub:{self.spec_id}"
