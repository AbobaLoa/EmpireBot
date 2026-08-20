from __future__ import annotations

from typing import Any, Protocol


class AttackMode(Protocol):
    spec_id: str

    def run_cycle(self) -> str:
        """Return a cycle result token. Must not click if the mode is a stub."""


class StubMode:
    def __init__(self, spec_id: str) -> None:
        self.spec_id = spec_id

    def run_cycle(self) -> str:
        return f"stub:{self.spec_id}"


def is_success_result(result: str, mode_id: str) -> bool:
    return result in {mode_id, "baron", "client:1"} or result.startswith("client:")
