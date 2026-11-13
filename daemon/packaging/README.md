# Packaging (Windows exe and autostart)

**Untested.** Written without a Windows machine. Nothing here has been built or run.

| File | Purpose |
|---|---|
| `build_exe.py` | PyInstaller one-file build wrapper, output `dist/ghostpad.exe` |
| `ghostpad_exe.py` | Frozen entry point: injects `--web-root`, handles no-console builds |
| `install_autostart.ps1` | Task Scheduler task at logon, hidden |
| `uninstall_autostart.ps1` | Removes that task |

No `.spec` file is committed: `.gitignore` ignores `*.spec` (and `build/`, `dist/`), so the
build is driven by `build_exe.py` flags; PyInstaller's generated spec lands in `build/`.

## Build

```
pip install pyinstaller vgamepad
python daemon\packaging\build_exe.py            # console build, shows logs
python daemon\packaging\build_exe.py --noconsole  # no window; log in %LOCALAPPDATA%\ghostpad\ghostpad.log
```

- `client/web` is staged (without `tests/`) and bundled at `<_MEIPASS>/client/web`.
- `DEFAULT_WEB_ROOT` in `ghostpad/server.py` resolves from `__file__` and is wrong when
  frozen, so `ghostpad_exe.py` adds `--web-root <_MEIPASS>/client/web` unless you pass your
  own. No change to the package was needed.
- `vgamepad` is collected with `--collect-all`. The exe does not install ViGEmBus; install
  the driver first (`docs/windows-setup.md`).
- One-file builds unpack to a temp dir at each start (slower start) and are often
  flagged by antivirus. If that is a problem, switch to `--onedir` in `build_exe.py`.

## Autostart

```
powershell -ExecutionPolicy Bypass -File daemon\packaging\install_autostart.ps1 -Token SECRET
Start-ScheduledTask -TaskName ghostpad      # start now without logging out
powershell -ExecutionPolicy Bypass -File daemon\packaging\uninstall_autostart.ps1
```

Parameters: `-Exe`, `-TaskName`, `-Port`, `-Bind`, `-Token`, `-ExtraArgs`. The task runs as
the current user at logon, restarts up to 3 times on failure and has no time limit. The
token is stored in the task definition. The firewall rule is separate
(`docs/windows-setup.md` section 5).
