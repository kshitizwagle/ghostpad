"""PyInstaller entry point for ghostpad.exe. Verified on Windows 11 (one-folder and one-file).

Why a wrapper instead of `-m ghostpad`: `DEFAULT_WEB_ROOT` in daemon/ghostpad/server.py is
computed from `__file__` (`parents[2] / "client" / "web"`), which is wrong inside a frozen
build (data lives under `sys._MEIPASS`: `_internal/` one-folder, `%TEMP%\\_MEI*` one-file).
Rather than touch the package, this wrapper injects `--web-root <_MEIPASS>/client/web` unless
the user passed one.

With a `--noconsole` build `sys.stdout`/`sys.stderr` are None; they are redirected to a log
file under %LOCALAPPDATA%\\ghostpad so logging does not break.
"""

from __future__ import annotations

import multiprocessing
import os
import sys
from pathlib import Path


def _redirect_if_no_console() -> None:
    if sys.stdout is not None and sys.stderr is not None:
        return
    base = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "ghostpad"
    base.mkdir(parents=True, exist_ok=True)
    log = open(base / "ghostpad.log", "a", encoding="utf-8", buffering=1)
    if sys.stdout is None:
        sys.stdout = log
    if sys.stderr is None:
        sys.stderr = log


def _inject_web_root() -> None:
    meipass = getattr(sys, "_MEIPASS", None)
    if not meipass:
        return  # not frozen: the package default is correct
    if any(a == "--web-root" or a.startswith("--web-root=") for a in sys.argv[1:]):
        return
    sys.argv[1:1] = ["--web-root", str(Path(meipass) / "client" / "web")]


if __name__ == "__main__":
    multiprocessing.freeze_support()
    _redirect_if_no_console()
    _inject_web_root()
    from ghostpad.__main__ import main

    sys.exit(main())
