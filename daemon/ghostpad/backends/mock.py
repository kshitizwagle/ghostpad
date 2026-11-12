from __future__ import annotations

from ..protocol import NEUTRAL, PadState
from .base import PadBackend


class MockBackend(PadBackend):
    """Records every call. `events` holds (op, pad, state|None) tuples in order."""

    name = "mock"
    max_pads = 4

    def __init__(self) -> None:
        self.events: list[tuple[str, int, PadState | None]] = []
        self.states: dict[int, PadState] = {}
        self.plugged: set[int] = set()
        self.closed = False

    def connect(self, pad: int) -> None:
        self.plugged.add(pad)
        self.states[pad] = NEUTRAL
        self.events.append(("connect", pad, None))

    def update(self, pad: int, state: PadState) -> None:
        self.states[pad] = state
        self.events.append(("update", pad, state))

    def neutral(self, pad: int) -> None:
        self.states[pad] = NEUTRAL
        self.events.append(("neutral", pad, NEUTRAL))

    def disconnect(self, pad: int) -> None:
        self.plugged.discard(pad)
        self.states.pop(pad, None)
        self.events.append(("disconnect", pad, None))

    def close(self) -> None:
        for pad in sorted(self.plugged):
            self.disconnect(pad)
        self.closed = True
        self.events.append(("close", -1, None))

    def ops(self) -> list[tuple[str, int]]:
        return [(op, pad) for op, pad, _ in self.events]

    def applied(self, pad: int | None = None) -> list[PadState]:
        return [s for op, p, s in self.events if op == "update" and (pad is None or p == pad) and s]
