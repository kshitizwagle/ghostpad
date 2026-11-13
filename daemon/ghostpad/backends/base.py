from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Callable

from ..protocol import PadState

# (pad, large, small), magnitudes 0-255. Backends may call it from any thread.
RumbleHandler = Callable[[int, int, int], None]


class BackendUnavailable(RuntimeError):
    """The backend cannot run here (missing package or driver). Message is user-facing."""


class PadBackend(ABC):
    name: str = "base"
    max_pads: int = 4
    _rumble_handler: RumbleHandler | None = None

    @abstractmethod
    def connect(self, pad: int) -> None:
        """Plug in the virtual pad."""

    @abstractmethod
    def update(self, pad: int, state: PadState) -> None:
        """Apply a full state to a plugged-in pad."""

    @abstractmethod
    def neutral(self, pad: int) -> None:
        """Release all buttons, triggers and sticks."""

    @abstractmethod
    def disconnect(self, pad: int) -> None:
        """Unplug the virtual pad."""

    @abstractmethod
    def close(self) -> None:
        """Release all resources (unplugs anything left)."""

    def set_rumble_handler(self, handler: RumbleHandler | None) -> None:
        """Where force-feedback changes from the game go. None stops forwarding."""
        self._rumble_handler = handler

    def _emit_rumble(self, pad: int, large: int, small: int) -> None:
        h = self._rumble_handler
        if h is not None:
            h(pad, large, small)
