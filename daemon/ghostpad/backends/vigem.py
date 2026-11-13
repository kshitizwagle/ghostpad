"""ViGEm backend (Windows, needs ViGEmBus). `vgamepad` is imported lazily."""

from __future__ import annotations

import logging
from typing import Any

from ..protocol import PadState
from .base import PadBackend

log = logging.getLogger(__name__)

# XUSB wButtons bits that exist (0x0800 is unused; LT/RT are analog only).
_BUTTON_MASK = 0xF7FF


class VigemBackend(PadBackend):
    name = "vigem"
    max_pads = 4

    def __init__(self) -> None:
        import vgamepad  # lazy: only available on Windows installs

        self._vg = vgamepad
        self._pads: dict[int, Any] = {}

    def connect(self, pad: int) -> None:
        if pad not in self._pads:
            self._pads[pad] = self._vg.VX360Gamepad()  # plugs in on creation
            log.info("vigem: pad %d plugged in", pad)

    def update(self, pad: int, state: PadState) -> None:
        gp = self._pads[pad]
        r = gp.report
        r.wButtons = state.buttons & _BUTTON_MASK
        r.bLeftTrigger = state.lt
        r.bRightTrigger = state.rt
        r.sThumbLX = state.lx
        r.sThumbLY = state.ly
        r.sThumbRX = state.rx
        r.sThumbRY = state.ry
        gp.update()

    def neutral(self, pad: int) -> None:
        gp = self._pads.get(pad)
        if gp is not None:
            gp.reset()
            gp.update()

    def disconnect(self, pad: int) -> None:
        gp = self._pads.pop(pad, None)
        if gp is not None:
            # vgamepad unplugs the target when the object is destroyed.
            del gp
            log.info("vigem: pad %d unplugged", pad)

    def close(self) -> None:
        for pad in list(self._pads):
            self.neutral(pad)
            self.disconnect(pad)
