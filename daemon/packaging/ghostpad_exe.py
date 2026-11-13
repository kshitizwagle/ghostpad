"""PyInstaller entry point for ghostpad.exe. Verified on Windows 11 (one-folder and one-file).

Why a wrapper instead of `-m ghostpad`: `DEFAULT_WEB_ROOT` in daemon/ghostpad/server.py is
computed from `__file__` (`parents[2] / "client" / "web"`), which is wrong inside a frozen
build (data lives under `sys._MEIPASS`: `_internal/` one-folder, `%TEMP%\\_MEI*` one-file).
Rather than touch the package, this wrapper injects `--web-root <_MEIPASS>/client/web` unless
the user passed one.

A windowed build (`build_exe.py --tray`) has no console, so it always runs as the tray app
(`--tray` is added), which logs to %LOCALAPPDATA%\\ghostpad\\ghostpad.log. `sys.stdout` and
`sys.stderr` are None there; they go to ghostpad-stderr.log next to it (stray prints and
tracebacks only), so the rotating log is never written through two handles.
"""

from __future__ import annotations

import multiprocessing
import os
import sys
from pathlib import Path


def _no_console() -> bool:
    return sys.stdout is None or sys.stderr is None


def _redirect_if_no_console() -> None:
    if not _no_console():
        return
    base = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "ghostpad"
    base.mkdir(parents=True, exist_ok=True)
    out = open(base / "ghostpad-stderr.log", "a", encoding="utf-8", buffering=1)
    if sys.stdout is None:
        sys.stdout = out
    if sys.stderr is None:
        sys.stderr = out


def _inject_web_root() -> None:
    meipass = getattr(sys, "_MEIPASS", None)
    if not meipass:
        return  # not frozen: the package default is correct
    if any(a == "--web-root" or a.startswith("--web-root=") for a in sys.argv[1:]):
        return
    sys.argv[1:1] = ["--web-root", str(Path(meipass) / "client" / "web")]


if __name__ == "__main__":
    multiprocessing.freeze_support()
    windowed = _no_console()
    _redirect_if_no_console()
    _inject_web_root()
    if windowed and "--tray" not in sys.argv[1:]:
        sys.argv.insert(1, "--tray")  # no console: the tray icon is the only UI
    from ghostpad.__main__ import main

    sys.exit(main())
