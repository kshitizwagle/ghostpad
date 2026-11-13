# Windows setup

First run of the daemon on Windows 10/11, with the verification checklist for issue #3.

**Status: daemon side verified** on Windows 11 Pro with ViGEmBus 1.17.333 (installed by
`vgamepad` 0.2.0), Python 3.12, PyInstaller 6.22: every check in section 4 passes through
XInput with `bench/vigem_check.py`, from source and from the packaged exe, plus the
autostart task. **Not verified:** a real controller through the browser client, and a real
game. Items marked UNVERIFIED are still from memory.

## 1. Prerequisites

| Item | Notes |
|---|---|
| Python 3.11+ | python.org installer, tick "Add python.exe to PATH". Check: `python --version` |
| ViGEmBus driver | See below |
| `vgamepad` | `pip install vgamepad` (PyPI package, wraps ViGEmClient) |
| Chrome or Edge | For the browser test client |
| A controller | Plugged into the PC (or paired by Bluetooth) for the first test |

### ViGEmBus

- Upstream: <https://github.com/nefarius/ViGEmBus> (releases page has the installer).
  The project is retired/archived by its author (see
  <https://docs.nefarius.at/projects/ViGEm/End-of-Life/>) but the last release still works.
- Known release tags seen in search results: `v1.21.442.0` and a later `1.22.x` build.
  **UNVERIFIED which is the recommended installer today**; prefer the newest release
  published on the upstream releases page.
- Alternative: Chocolatey package `vigembus` (<https://community.chocolatey.org/packages/vigembus>).
- `pip install vgamepad` installs the driver itself (read from the `vgamepad` 0.2.0 sdist):
  it is sdist-only, and its `setup.py` runs `msiexec /i ViGEmBusSetup_x64.msi` (bundled
  ViGEmBus **1.17.333**, interactive, UAC) unless "Nefarius Virtual Gamepad Emulation Bus
  Driver" is already in the Uninstall registry. A different installed version only gives a
  warning. To skip the bundled installer (e.g. you installed a newer release first), set
  `VGAMEPAD_SKIP_VIGEMBUS_INSTALL=true` before `pip install`.
- Reboot if the installer asks. Device Manager should then list "Nefarius Virtual Gamepad
  Emulation Bus" under System devices.

## 2. Run the daemon

Create a virtualenv if you like, then from the repo root:

```
pip install vgamepad
cd daemon
python -m ghostpad --verbose
```

Expect log lines about the server listening on `0.0.0.0:47800` and the `vigem` backend.
If it logs "falling back to the mock backend", the warning says why: `vgamepad` is not
installed, or it cannot reach the driver (`VIGEM_ERROR_BUS_NOT_FOUND`; `vgamepad` connects
to the bus when imported, so this shows at startup). The web client then shows `mock` next
to the status. `--backend vigem` exits with code 2 instead of falling back.

Stop with Ctrl+C or Ctrl+Break: pads go neutral, are unplugged, and exit stats are logged
(Ctrl+Break verified with the mock backend). Closing the console window or killing the
process skips that; the driver then removes the pads within a few ms (check 18).

Options: `--port`, `--bind` (use `127.0.0.1` for local-only), `--token SECRET`,
`--backend vigem|mock|auto`, `--trace` (log every pad operation at DEBUG, useful for the checklist below). `--token` is recommended whenever the port is open to the LAN.

## 3. First test on the PC

1. Open <http://localhost:47800> in Chrome/Edge (secure context: `localhost` only).
2. Connect a controller and press any button (browsers hide the pad until then).
3. The page should show a connected status and the pad's live state.
4. Press `Win+R`, run `joy.cpl`. "Xbox 360 Controller for Windows" appears after the first
   input. Properties shows live values.

## 4. Verification checklist (issue #3)

Two ways to run it:

- **Automated, daemon side:** `python bench/vigem_check.py` (or `--exe
  daemon\packaging\dist\ghostpad\ghostpad.exe`). It starts the daemon with the vigem
  backend, drives it with a protocol client and reads the pads back with XInput (the API
  games use), acting as the game for rumble. No controller or browser involved. Results
  below: 2026-11-13, 3 runs (2 from source, 1 exe). All 16 checks passed in the last
  two; in the first, `client_killed` failed from a harness bug (the reset was not flushed
  before measuring), fixed since.
- **By hand, with a controller:** use the `joy.cpl` Properties tab or a controller tester
  site in a **separate** browser window (not the ghostpad page). This also covers the
  browser's Gamepad API mapping and `vibrationActuator`, which the script does not.

| # | Check | Expected | `vigem_check` | Controller + browser |
|---|---|---|---|---|
| 1 | A, B, X, Y | Each lights its own button, no swaps | pass | |
| 2 | LB, RB | Shoulder buttons 5 and 6 (as shown by the tester) | pass | |
| 3 | Back, Start | Back/View and Start/Menu | pass | |
| 4 | LS click, RS click | Stick-click buttons | pass | |
| 5 | D-pad up/down/left/right | POV hat directions, each correct | pass | |
| 6 | Guide (if your pad sends it) | Guide/Xbox button; may be swallowed by Windows | pass (read with XInputGetStateEx) | |
| 7 | LT, RT | Analog 0 to full travel, independent of each other, no button bit | pass, 0-255 exact | |
| 8 | Left stick | Up = up, down = down, left = left, right = right; full range reaches the extremes | pass, exact incl. extremes | |
| 9 | Right stick | Same as left stick (Y inversion correct on both) | pass | |
| 10 | Neutral | Release everything: all axes return to centre, triggers to 0 | pass | |
| 11 | Unplug on client disconnect | Close the ghostpad tab: the Xbox 360 pad disappears from `joy.cpl` (and Device Manager) within a moment | pass, 1.9-3.2 ms after close | |
| 12 | Reconnect | Reopen the tab, press a button: pad reappears and works | pass | |
| 13 | Watchdog, hidden tab | Hold a button or push a stick, switch to another tab/minimise the window so the page stops polling (rAF is throttled in hidden tabs): the held input releases to neutral within about 250 ms and the pad stays plugged in | pass, release max 215.3 ms (10 trials) | |
| 14 | Watchdog resumes | Bring the tab back, press a button: input resumes without reconnecting | pass | |
| 15 | Takeover | Open a second tab/client: the first is closed with code 4001, second drives the pad | pass | |
| 16 | Stuck button after kill | Hold a button, end the browser process in Task Manager: input releases (watchdog or close) | pass, connection reset: unplug 0.9 ms | |
| 17 | Rumble | In a game (or tester) that vibrates the pad: the controller rumbles, the ghostpad page shows L/S values, `/stats` `rumble_sent` rises, and vibration stops when the game stops it. Run with `--trace` to see `rumble large= small=` lines | pass: XInputSetState(L, R) gives RUMBLE large = L >> 8, small = R >> 8 in 0.1-0.24 ms | |
| 18 | Daemon killed | Hold a button, end `python`/`ghostpad.exe` in Task Manager: the virtual pad disappears from `joy.cpl` (driver cleanup; the daemon cannot release on a hard kill) | pass, driver removes the pad 2.1-2.2 ms after the kill | |
| 19 | Daemon Ctrl+Break | Hold a button, press Ctrl+Break in the daemon console: pad neutral, then unplugged; page shows disconnected | pass, unplug 1.1-1.3 ms, exit 0 | |

Note for 13: the watchdog is fed only by STATE and PAD_GONE frames; PINGs and WebSocket
ping/pong do not count, so a stalled poll loop cannot be masked. Measured on this Windows 11
machine, last STATE to neutral: mock backend 100 trials median 203.0 ms, max 215.5 ms;
vigem via XInput 3 x 10 trials, max 215.3 ms.

`vigem_check` also covers four pads at once (pads 0-3 map to XInput slots 0-3, each reads
its own state) and STATE-sent to XInput-visible latency on loopback: median 0.100-0.225 ms,
p99 0.189-0.528 ms (3 runs, 200-500 samples).

Latency numbers are a separate exercise: `docs/latency-testing.md`.

## 5. Allow other machines (firewall)

Needed for the TV or another PC. Run in an **Administrator** PowerShell, with the port
you use (default 47800). Limit it to the Private profile:

```
New-NetFirewallRule -DisplayName "ghostpad" -Direction Inbound -Protocol TCP `
  -LocalPort 47800 -Action Allow -Profile Private
```

Equivalent: `netsh advfirewall firewall add rule name="ghostpad" dir=in action=allow protocol=TCP localport=47800 profile=private`.

Remove with `Remove-NetFirewallRule -DisplayName "ghostpad"`. Windows may also show an
"allow access" dialog the first time Python or `ghostpad.exe` listens; if you ticked only
"Public networks" by mistake, fix it in Windows Security > Firewall > Allow an app. Check
that your LAN is classed as Private (Settings > Network > Properties).

## 6. Find the PC's LAN IP

```
ipconfig
```

Use the IPv4 Address of the Ethernet (or Wi-Fi) adapter, e.g. `192.168.1.50`. Reserve it in
your router (DHCP reservation) so it does not change. From another machine, check
`http://192.168.1.50:47800/stats` returns JSON (append `?token=SECRET` if the daemon runs with `--token`).

For the browser client on another machine, the Gamepad API will not work (insecure
context); see the README. The Tizen app is the intended remote client.

## 7. Autostart at login

Option A, tray app (recommended): build it with `python daemon\packaging\build_exe.py --tray`,
start `daemon\packaging\dist\ghostpad-tray\ghostpad-tray.exe`, then tick **Start with
Windows** in its tray menu (HKCU Run key, no admin). Quit from the same menu releases the
pads. Details: `daemon/packaging/README.md`. Without building: `pythonw -m ghostpad --tray`
from `daemon/` runs the same tray app (the menu's autostart item needs the exe).

Option B, packaged exe plus Task Scheduler: see `daemon/packaging/README.md`
(`build_exe.py`, `install_autostart.ps1`). Use A or B, not both: the second start fails on
the busy port.

Option C, no exe, Startup folder: press `Win+R`, run `shell:startup`, and create
`ghostpad.cmd` there:

```
@echo off
cd /d C:\path\to\ghostpad\daemon
start "" /min pythonw -m ghostpad --token SECRET
```

`pythonw` has no console, so there is no log output and errors are invisible; add `--tray`
(log in `%LOCALAPPDATA%\ghostpad\ghostpad.log`, errors in a message box), or use `python`
with `start /min` while debugging. Startup-folder items run at user logon only.
(`pythonw` and Startup-folder behaviour: UNVERIFIED on this setup.)

## 8. Troubleshooting

| Symptom | Likely cause and fix |
|---|---|
| "vgamepad is not installed" (warning with `auto`, exit 2 with `--backend vigem`) | Not installed in the Python you run: `python -m pip install vgamepad` |
| "cannot connect to the ViGEmBus driver (Error: VIGEM_ERROR_BUS_NOT_FOUND)" at startup | ViGEmBus not installed or service stopped. Install it, reboot, check Device Manager |
| `cannot start server: [Errno 10048]` (address already in use) | Another process on 47800: a second daemon, or something else. `netstat -ano \| findstr 47800`, then `taskkill /PID <pid> /F`, or pick `--port 47801` |
| Works on `localhost`, not from the TV or LAN | Firewall rule missing, network profile is Public, wrong IP, or `--bind 127.0.0.1` |
| Page loads from LAN IP but pad never reacts | Insecure context: Gamepad API hidden. Use `localhost` on the PC |
| No pad in `joy.cpl` | Pads are plugged in lazily on the first STATE. Press a button. Check the daemon log shows a client connected and the `vigem` backend |
| Pad shows in `joy.cpl` but a game ignores it | Game wants XInput: the virtual pad is X360, so it should work. Check Steam Input (below), and that the game is not set to a different controller index |
| Double inputs or phantom second pad in Steam games | Steam Input also maps the real controller on the PC, or Steam wraps the virtual pad. In Steam: Settings > Controller, disable "Enable Steam Input for Xbox controllers" or disable the real controller, so only the ghostpad pad is active. Do not run the real pad and ghostpad into the same game |
| Input stops after a few seconds with the tab in the background | Browsers throttle hidden tabs; the watchdog releases to neutral by design. Keep the tab visible, or use the Tizen app |
| Another client "takes over" unexpectedly | One client owns the pads; a new connection closes the old one (code 4001) |
| Connection refused with a token set | Token mismatch: connect with `?token=SECRET` or the token field |

## Not verified

- The "Controller + browser" column of section 4, and a real game.
- Autostart at an actual logon (verified with `Start-ScheduledTask`, same task).
- Whether the newest upstream ViGEmBus works with `vgamepad`'s bundled ViGEmClient
  (`vgamepad` only warns on a version mismatch).
- The Startup-folder option (7B) and `pythonw`.
