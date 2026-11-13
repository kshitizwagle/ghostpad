"""Build the ghostpad Windows executable with PyInstaller (one folder by default).

Run on Windows from anywhere:

    pip install pyinstaller vgamepad
    python daemon/packaging/build_exe.py [--tray] [--onefile] [--name NAME]

Output (gitignored, as are build/ and the generated .spec):
  default     daemon/packaging/dist/ghostpad/ghostpad.exe  (+ _internal/; ship the folder)
  --tray      daemon/packaging/dist/ghostpad-tray/ghostpad-tray.exe
  --onefile   daemon/packaging/dist/<name>.exe

Default is a console exe (log in the window). `--tray` builds the windowed tray app; it has
its own default name, so both builds can sit side by side. Its log is in
%LOCALAPPDATA%\\ghostpad. Both get the controller icon, generated from ghostpad/trayicon.py.

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
    ap.add_argument("--name", default=None, help="default: ghostpad, or ghostpad-tray with --tray")
    ap.add_argument("--tray", "--noconsole", dest="tray", action="store_true",
                    help="windowed tray app (no console; log in %%LOCALAPPDATA%%\\ghostpad\\ghostpad.log)")
    ap.add_argument("--onefile", action="store_true",
                    help="single portable exe (see the module docstring for the stop/temp-dir caveat)")
    ap.add_argument("--clean", action="store_true", help="remove build/ and dist/ first")
    ap.add_argument("--allow-no-vgamepad", action="store_true",
                    help="build even if vgamepad is not installed (the exe then only has the mock backend)")
    args = ap.parse_args()
    args.name = args.name or ("ghostpad-tray" if args.tray else "ghostpad")

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

    sys.path.insert(0, str(DAEMON))
    from ghostpad.trayicon import ico_bytes  # pure Python: same artwork as the tray icon

    icon = build / "ghostpad.ico"
    icon.write_bytes(ico_bytes("connected"))

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
        "--hidden-import", "ghostpad.tray",
        "--icon", str(icon),
    ]
    if args.tray:
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
