"""Backend selection and the vigem backend against a fake `vgamepad` (no Windows needed)."""

import builtins
import inspect
import logging
import sys
import types
import unittest
import weakref
from unittest import mock

from ghostpad import protocol as p
from ghostpad.__main__ import main, make_backend
from ghostpad.backends.base import BackendUnavailable
from ghostpad.backends.mock import MockBackend
from ghostpad.backends.vigem import VigemBackend

S1 = p.PadState(0x1000 | 0x0800, 255, 128, 32767, -32767, 1, -1)


class FakeReport:
    def __init__(self) -> None:
        self.wButtons = self.bLeftTrigger = self.bRightTrigger = 0
        self.sThumbLX = self.sThumbLY = self.sThumbRX = self.sThumbRY = 0


class FakeX360:
    """Mimics vgamepad 0.2.0's VX360Gamepad: plugs in on creation, unplugs in __del__."""

    live: set[int] = set()
    _n = 0

    def __init__(self) -> None:
        FakeX360._n += 1
        self.id = FakeX360._n
        self.report = FakeReport()
        self.sent: list[tuple] = []
        self.cmp_func = None
        FakeX360.live.add(self.id)

    def register_notification(self, callback_function) -> None:
        # vgamepad.api.check_callback_signature: must bind 6 positional arguments
        inspect.signature(callback_function).bind(*range(6))
        self.cmp_func = callback_function

    def update(self) -> None:
        r = self.report
        self.sent.append((r.wButtons, r.bLeftTrigger, r.bRightTrigger,
                          r.sThumbLX, r.sThumbLY, r.sThumbRX, r.sThumbRY))

    def reset(self) -> None:
        self.report = FakeReport()

    def __del__(self) -> None:
        FakeX360.live.discard(self.id)


def fake_vgamepad() -> types.ModuleType:
    m = types.ModuleType("vgamepad")
    m.VX360Gamepad = FakeX360
    return m


def import_raising(exc: BaseException):
    real = builtins.__import__

    def fake(name, *a, **k):
        if name == "vgamepad":
            raise exc
        return real(name, *a, **k)
    return mock.patch("builtins.__import__", fake)


class LoadTests(unittest.TestCase):
    def test_not_installed(self):
        with mock.patch.dict(sys.modules, {"vgamepad": None}):
            with self.assertRaisesRegex(BackendUnavailable, "not installed"):
                VigemBackend()

    def test_driver_missing(self):
        # vgamepad connects to the bus at import time and raises RuntimeError without ViGEmBus
        with import_raising(RuntimeError("Error: VIGEM_ERROR_BUS_NOT_FOUND")):
            with self.assertRaisesRegex(BackendUnavailable, "ViGEmBus.*BUS_NOT_FOUND"):
                VigemBackend()

    def test_dll_load_failure(self):
        with import_raising(OSError("[WinError 126] module not found")):
            with self.assertRaises(BackendUnavailable):
                VigemBackend()


class SelectionTests(unittest.TestCase):
    def test_auto_on_windows_falls_back_when_driver_missing(self):
        with mock.patch.object(sys, "platform", "win32"), \
                import_raising(RuntimeError("Error: VIGEM_ERROR_BUS_NOT_FOUND")), \
                self.assertLogs("ghostpad", logging.WARNING) as cm:
            b = make_backend("auto")
        self.assertIsInstance(b, MockBackend)
        self.assertIn("falling back to the mock backend", cm.output[0])

    def test_auto_on_windows_uses_vigem(self):
        with mock.patch.object(sys, "platform", "win32"), \
                mock.patch.dict(sys.modules, {"vgamepad": fake_vgamepad()}):
            self.assertIsInstance(make_backend("auto"), VigemBackend)

    def test_explicit_vigem_exits_2(self):
        with import_raising(RuntimeError("Error: VIGEM_ERROR_BUS_NOT_FOUND")), \
                mock.patch("logging.basicConfig"), \
                self.assertLogs("ghostpad", logging.ERROR) as cm:
            rc = main(["--backend", "vigem", "--bind", "127.0.0.1", "--port", "0"])
        self.assertEqual(rc, 2)
        self.assertIn("ViGEmBus", cm.output[0])


class VigemBackendTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(sys.modules, {"vgamepad": fake_vgamepad()})
        patcher.start()
        self.addCleanup(patcher.stop)
        FakeX360.live.clear()
        self.b = VigemBackend()

    def test_update_maps_fields_and_masks_unused_bit(self):
        self.b.connect(0)
        self.b.update(0, S1)
        gp = self.b._pads[0]
        self.assertEqual(gp.sent[-1], (0x1000, 255, 128, 32767, -32767, 1, -1))

    def test_neutral_sends_zero_report(self):
        self.b.connect(0)
        self.b.update(0, S1)
        self.b.neutral(0)
        self.assertEqual(self.b._pads[0].sent[-1], (0, 0, 0, 0, 0, 0, 0))

    def test_disconnect_unplugs_immediately(self):
        # The rumble callback must not form a cycle with the gamepad, or the unplug in
        # vgamepad's __del__ would wait for the garbage collector.
        self.b.set_rumble_handler(lambda *a: None)
        self.b.connect(0)
        ref = weakref.ref(self.b._pads[0])
        self.b.disconnect(0)
        self.assertIsNone(ref())
        self.assertEqual(FakeX360.live, set())

    def test_rumble_callback_forwards_pad_and_motors(self):
        got = []
        self.b.set_rumble_handler(lambda *a: got.append(a))
        self.b.connect(2)
        cb = self.b._pads[2].cmp_func
        cb(None, None, 200, 30, 0, None)  # client, target, large, small, led, user_data
        self.assertEqual(got, [(2, 200, 30)])

    def test_rumble_callback_never_raises(self):
        def boom(*a):
            raise ValueError("handler bug")
        self.b.set_rumble_handler(boom)
        self.b.connect(0)
        self.b._pads[0].cmp_func(None, None, 1, 1, 0, None)  # no exception

    def test_close_unplugs_all_and_detaches_handler(self):
        self.b.set_rumble_handler(lambda *a: None)
        for pad in range(4):
            self.b.connect(pad)
        self.b.close()
        self.assertEqual(FakeX360.live, set())
        self.assertIsNone(self.b._rumble_handler)


if __name__ == "__main__":
    unittest.main()
