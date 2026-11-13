"""Build dist/ghostpad.exe with PyInstaller (one file). UNTESTED on Windows.

Run on Windows from anywhere:

    pip install pyinstaller vgamepad
    python daemon/packaging/build_exe.py [--noconsole] [--name ghostpad]

Output: daemon/packaging/dist/ghostpad.exe. Work files go to daemon/packaging/build/.
Both are gitignored (`build/`, `dist/`). The generated `.spec` is written into build/ and
is ignored too (`*.spec` is in .gitignore), which is why this is a script and not a
committed spec file.
"""

from __future__ import annotations

import argparse
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
    ap.add_argument("--clean", action="store_true", help="remove build/ and dist/ first")
    args = ap.parse_args()

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
        "--noconfirm", "--onefile",
        "--name", args.name,
        "--distpath", str(dist),
        "--workpath", str(build / "work"),
        "--specpath", str(build),
        "--paths", str(DAEMON),
        "--add-data", f"{stage}{sep}client/web",   # lands at <_MEIPASS>/client/web
        # vgamepad is imported lazily and ships a DLL/driver installer as data; collect it all
        "--collect-all", "vgamepad",
        "--hidden-import", "ghostpad.backends.vigem",
        "--hidden-import", "ghostpad.backends.mock",
    ]
    if args.noconsole:
        cmd.append("--noconsole")
    cmd.append(str(HERE / "ghostpad_exe.py"))

    print(" ".join(cmd))
    rc = subprocess.call(cmd)
    if rc == 0:
        print(f"\nbuilt: {dist / (args.name + ('.exe' if sys.platform == 'win32' else ''))}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
