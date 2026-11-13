"""ViGEm backend (Windows, needs ViGEmBus). `vgamepad` is imported lazily."""

from __future__ import annotations

import logging
from typing import Any, Callable

from ..protocol import PadState
from .base import BackendUnavailable, PadBackend

log = logging.getLogger(__name__)

# XUSB wButtons bits that exist (0x0800 is unused; LT/RT are analog only).
_BUTTON_MASK = 0xF7FF


def _load_vgamepad() -> Any:
    """Import vgamepad. It connects to ViGEmBus at import time (vgamepad 0.2.0), so a missing
    driver raises RuntimeError('Error: VIGEM_ERROR_BUS_NOT_FOUND'), not ImportError."""
    try:
        import vgamepad  # lazy: only available on Windows installs
    except ImportError as e:
        raise BackendUnavailable(f"vgamepad is not installed ({e}); run: pip install vgamepad") from e
    except (RuntimeError, OSError) as e:
        raise BackendUnavailable(
            f"cannot connect to the ViGEmBus driver ({e}); install it, see docs/windows-setup.md"
        ) from e
    return vgamepad


class VigemBackend(PadBackend):
    name = "vigem"
    max_pads = 4

    def __init__(self) -> None:
        self._vg = _load_vgamepad()
        self._pads: dict[int, Any] = {}

    def connect(self, pad: int) -> None:
        if pad in self._pads:
            return
        gp = self._vg.VX360Gamepad()  # plugs in on creation
        try:
            gp.register_notification(self._rumble_callback(pad))
        except Exception:
            log.warning("vigem: rumble unavailable for pad %d", pad, exc_info=True)
        self._pads[pad] = gp
        log.info("vigem: pad %d plugged in", pad)

    def _rumble_callback(self, pad: int) -> Callable[..., None]:
        # Called on a ViGEmClient thread. Must not reference the gamepad object: vgamepad
        # holds this callback on the gamepad, and a cycle would delay the unplug in __del__.
        emit = self._emit_rumble

        def cb(client: Any, target: Any, large_motor: int, small_motor: int,
               led_number: int, user_data: Any) -> None:
            try:
                emit(pad, large_motor, small_motor)
            except Exception:
                pass  # never raise into the driver's thread

        return cb

    def update(self, pad: int, state: PadState) -> None:
        gp = self._pads[pad]
        r = gp.report
        r.wButtons = state.buttons & _BUTTON_MASK
        # bLeftTrigger/bRightTrigger are ctypes c_byte (signed); 0-255 wraps to the same byte.
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
            # vgamepad unregisters the rumble callback and unplugs the target in __del__,
            # which runs here because nothing else references the object.
            del gp
            log.info("vigem: pad %d unplugged", pad)

    def close(self) -> None:
        self.set_rumble_handler(None)
        for pad in list(self._pads):
            self.neutral(pad)
            self.disconnect(pad)
