"""Build the ghostpad Windows executable with PyInstaller (one folder by default).

Run on Windows from anywhere:

    pip install pyinstaller vgamepad
    python daemon/packaging/build_exe.py [--noconsole] [--onefile] [--name ghostpad]

Output (gitignored, as are build/ and the generated .spec):
  default     daemon/packaging/dist/ghostpad/ghostpad.exe  (+ _internal/; ship the folder)
  --onefile   daemon/packaging/dist/ghostpad.exe

Why one folder by default: a one-file exe is a bootloader that unpacks to %TEMP%\\_MEI* and
runs the daemon as a child process. Killing the bootloader (Stop-ScheduledTask, Task Manager,
taskkill /F) leaves the child running and holding the port, and leaks the ~9 MB temp folder.
Measured on Windows 11 with PyInstaller 6.22. The one-folder build is a single process.
"""

from __future__ import annotations

import argparse
import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
DAEMON = HERE.parent
REPO = DAEMON.parent
WEB_SRC = REPO / "client" / "web"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--name", default="ghostpad")
    ap.add_argument("--noconsole", action="store_true",
                    help="windowed build (no console; log goes to %%LOCALAPPDATA%%\\ghostpad\\ghostpad.log)")
    ap.add_argument("--onefile", action="store_true",
                    help="single portable exe (see the module docstring for the stop/temp-dir caveat)")
    ap.add_argument("--clean", action="store_true", help="remove build/ and dist/ first")
    ap.add_argument("--allow-no-vgamepad", action="store_true",
                    help="build even if vgamepad is not installed (the exe then only has the mock backend)")
    args = ap.parse_args()

    # Without vgamepad, PyInstaller only warns and the exe silently falls back to mock.
    if importlib.util.find_spec("vgamepad") is None and not args.allow_no_vgamepad:
        print("vgamepad is not installed in this Python; the exe would only have the mock "
              "backend.\nRun `pip install vgamepad` first, or pass --allow-no-vgamepad.",
              file=sys.stderr)
        return 1

    build, dist = HERE / "build", HERE / "dist"
    if args.clean:
        shutil.rmtree(build, ignore_errors=True)
        shutil.rmtree(dist, ignore_errors=True)
    if not WEB_SRC.is_dir():
        print(f"missing {WEB_SRC}", file=sys.stderr)
        return 1

    # Stage the web client without the node tests, so only the served files are bundled.
    stage = build / "web"
    shutil.rmtree(stage, ignore_errors=True)
    build.mkdir(parents=True, exist_ok=True)
    shutil.copytree(WEB_SRC, stage, ignore=shutil.ignore_patterns("tests", "node_modules", "*.test.*"))

    sep = ";" if sys.platform == "win32" else ":"  # --add-data separator
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--onefile" if args.onefile else "--onedir",
        "--name", args.name,
        "--distpath", str(dist),
        "--workpath", str(build / "work"),
        "--specpath", str(build),
        "--paths", str(DAEMON),
        "--add-data", f"{stage}{sep}client/web",   # lands at <_MEIPASS>/client/web
        # vgamepad is imported lazily. Only its modules and ViGEmClient.dll are needed; not
        # --collect-all, which also packs the ViGEmBus MSIs (~1 MB) and has to import vgamepad,
        # which fails on a build machine without the driver (it connects at import time).
        "--collect-binaries", "vgamepad",
        "--hidden-import", "vgamepad.api",
        "--hidden-import", "vgamepad.win.vigem_client",
        "--hidden-import", "vgamepad.win.virtual_gamepad",
        "--hidden-import", "ghostpad.backends.vigem",
        "--hidden-import", "ghostpad.backends.mock",
    ]
    if args.noconsole:
        cmd.append("--noconsole")
    cmd.append(str(HERE / "ghostpad_exe.py"))

    print(" ".join(cmd))
    rc = subprocess.call(cmd)
    if rc == 0:
        exe = args.name + (".exe" if sys.platform == "win32" else "")
        print(f"\nbuilt: {dist / exe if args.onefile else dist / args.name / exe}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
