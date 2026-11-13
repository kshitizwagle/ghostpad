from __future__ import annotations

from abc import ABC, abstractmethod

from ..protocol import PadState


class PadBackend(ABC):
    name: str = "base"
    max_pads: int = 4

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
