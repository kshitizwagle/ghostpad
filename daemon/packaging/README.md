# Packaging (Windows exe and autostart)

Verified on Windows 11 Pro, Python 3.12, PyInstaller 6.22, `vgamepad` 0.2.0. Without the
ViGEmBus driver: builds, serves the bundled web client, runs the mock backend under load
(`bench/bench.py`), reports the missing driver. With ViGEmBus 1.17.333: the one-folder exe
passes all 16 `bench/vigem_check.py --exe` checks, and under the autostart task (no-console
build, default `auto` backend) a client's pad shows up in XInput and unplugs on close.

| File | Purpose |
|---|---|
| `build_exe.py` | PyInstaller build wrapper; console or `--tray`; one folder by default, `--onefile` optional |
| `ghostpad_exe.py` | Frozen entry point: injects `--web-root`; a windowed build always runs `--tray` |
| `install_autostart.ps1` | Task Scheduler task at logon, hidden |
| `uninstall_autostart.ps1` | Stops the daemon and removes that task |

No `.spec` file is committed: `.gitignore` ignores `*.spec` (and `build/`, `dist/`), so the
build is driven by `build_exe.py` flags; PyInstaller's generated spec lands in `build/`.

## Build

```
pip install pyinstaller vgamepad
python daemon\packaging\build_exe.py              # dist\ghostpad\ghostpad.exe, console, shows logs
python daemon\packaging\build_exe.py --tray       # dist\ghostpad-tray\ghostpad-tray.exe, tray app (below)
python daemon\packaging\build_exe.py --onefile    # dist\ghostpad.exe, single file (see below)
```

Both builds can coexist (different folders) and both carry the controller icon, generated
from `ghostpad/trayicon.py` at build time (`build\ghostpad.ico`). `--noconsole` is an alias
of `--tray`.

- **One folder is the default.** Ship the whole `dist\ghostpad\` folder (~20 MB). A one-file
  exe (~9 MB) is a bootloader that unpacks to `%TEMP%\_MEI*` and runs the daemon as a child
  process. Measured: killing the bootloader (Task Manager, `Stop-ScheduledTask`) left the
  child running and serving, and every hard stop leaked a ~9 MB `_MEI*` folder.
- The build refuses to run without `vgamepad` (the exe would silently be mock-only); pass
  `--allow-no-vgamepad` to build anyway.
- `vgamepad` is collected as its modules plus `ViGEmClient.dll` only. `--collect-all` is
  avoided: it also packs the ViGEmBus MSIs (~1 MB) and imports `vgamepad`, which fails on a
  build machine without the driver. The exe does not install ViGEmBus; install the driver
  first (`docs/windows-setup.md`).
- `client/web` is staged without `tests/` and bundled at `<_MEIPASS>/client/web`
  (`_internal\client\web` in the one-folder build). `ghostpad_exe.py` passes it as
  `--web-root` unless you give your own.
- Without the driver: `--backend auto` logs the reason and falls back to mock;
  `--backend vigem` exits with code 2.

## Tray app

`ghostpad-tray.exe` (or `python -m ghostpad --tray`) runs the daemon with a notification-area
icon and no console. Same flags as the console daemon; it is independent of any terminal, so
closing one does not stop it.

| Icon colour | Meaning |
|---|---|
| Grey | Listening, no client |
| Green | A client owns the pads (tooltip: client name and players) |
| Amber | `mock` backend: ViGEmBus unavailable, no virtual pad (tooltip says so) |
| Red | Daemon stopped; a balloon names the log |

Menu (left or right click): status lines, **Open log**, **Open web test client**
(`http://localhost:<port>/`, with `?token=`), **Start with Windows**, **Quit**.

- Log: `%LOCALAPPDATA%\ghostpad\ghostpad.log`, rotated at 1 MB, 3 kept (`--log-file` to move
  it). Stray output of the windowed exe goes to `ghostpad-stderr.log` beside it.
- **Quit**, Windows logoff/shutdown and Ctrl+C (console runs) stop the server normally:
  pads neutral, then unplug. A kill still relies on the driver unplugging on exit.
- **Start with Windows** writes `HKCU\Software\Microsoft\Windows\CurrentVersion\Run\ghostpad`
  (no admin) with this exe, `--tray` and the current `--port`, `--bind`, `--backend`,
  `--token`, `--log-file`. The token is stored there in plain text. Exe builds only. Do not
  combine with the Task Scheduler task below: the second start fails on the busy port.
- Start-up errors (port in use, `--backend vigem` without the driver) show a message box.
  A second tray app on the same port is refused.
- If Explorer restarts, the icon is added again. At logon, adding is retried every second
  until the taskbar exists.
- Rebuilding while it runs fails (files in use): Quit first.

Verified on Windows 11: icon added (grey with `vigem`, amber with `mock`), status snapshot
with a client and pad, WM_CLOSE quit with the normal release and exit 0
(`daemon/tests/test_tray.py`, and the same by hand against the built exe), and the TV client
reconnecting to it with the `vigem` backend. Not verified by a person: the menu, colour
changes on screen, a real logon start from the Run key, release at a real logoff.

## Autostart

```
powershell -ExecutionPolicy Bypass -File daemon\packaging\install_autostart.ps1 -Token SECRET
Start-ScheduledTask -TaskName ghostpad      # start now without logging out
powershell -ExecutionPolicy Bypass -File daemon\packaging\uninstall_autostart.ps1
```

Parameters: `-Exe` (default: `dist\ghostpad\ghostpad.exe`, else `dist\ghostpad.exe`),
`-TaskName`, `-Port`, `-Bind`, `-Token`, `-ExtraArgs`. The task runs as the current user at
logon (registering needs no admin), restarts up to 3 times on failure and has no time limit.
The token is stored in plain text in the task definition. The firewall rule is separate
(`docs/windows-setup.md` section 5).

Stopping the task ends the process without the daemon's neutral-then-unplug release (a
no-console process gets no Ctrl+C). Held inputs then depend on the driver unplugging the
pads when the process exits: measured 2.1-2.2 ms (`docs/windows-setup.md` check 18).

`uninstall_autostart.ps1` also stops any process still running the task's exe after a short
wait (the one-file child above). Not verified: an actual logon start.
