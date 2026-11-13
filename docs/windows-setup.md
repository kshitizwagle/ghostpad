# Windows setup

First run of the daemon on Windows 10/11, with the verification checklist for issue #3.

**Status: partly verified** on Windows 11 Pro, Python 3.12, PyInstaller 6.22, *without*
the ViGEmBus driver: the daemon with the mock backend, all test suites, the missing-driver
handling, `vgamepad` 0.2.0 install behaviour, the exe build and the autostart scripts.
**Not verified:** anything that needs the driver (the issue #3 checklist below). Items
marked UNVERIFIED are still from memory.

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
process skips that; whether the driver then unplugs the pads is UNVERIFIED (check 18).

Options: `--port`, `--bind` (use `127.0.0.1` for local-only), `--token SECRET`,
`--backend vigem|mock|auto`, `--trace` (log every pad operation at DEBUG, useful for the checklist below). `--token` is recommended whenever the port is open to the LAN.

## 3. First test on the PC

1. Open <http://localhost:47800> in Chrome/Edge (secure context: `localhost` only).
2. Connect a controller and press any button (browsers hide the pad until then).
3. The page should show a connected status and the pad's live state.
4. Press `Win+R`, run `joy.cpl`. "Xbox 360 Controller for Windows" appears after the first
   input. Properties shows live values.

## 4. Verification checklist (issue #3)

Record pass/fail and notes. Use the `joy.cpl` Properties tab or a controller tester site
in a **separate** browser window (not the ghostpad page, and do not use the same
controller read by two pages if it confuses you).

| # | Check | Expected | Result |
|---|---|---|---|
| 1 | A, B, X, Y | Each lights its own button, no swaps | |
| 2 | LB, RB | Shoulder buttons 5 and 6 (as shown by the tester) | |
| 3 | Back, Start | Back/View and Start/Menu | |
| 4 | LS click, RS click | Stick-click buttons | |
| 5 | D-pad up/down/left/right | POV hat directions, each correct | |
| 6 | Guide (if your pad sends it) | Guide/Xbox button; may be swallowed by Windows | |
| 7 | LT, RT | Analog 0 to full travel, independent of each other, no button bit | |
| 8 | Left stick | Up = up, down = down, left = left, right = right; full range reaches the extremes | |
| 9 | Right stick | Same as left stick (Y inversion correct on both) | |
| 10 | Neutral | Release everything: all axes return to centre, triggers to 0 | |
| 11 | Unplug on client disconnect | Close the ghostpad tab: the Xbox 360 pad disappears from `joy.cpl` (and Device Manager) within a moment | |
| 12 | Reconnect | Reopen the tab, press a button: pad reappears and works | |
| 13 | Watchdog, hidden tab | Hold a button or push a stick, switch to another tab/minimise the window so the page stops polling (rAF is throttled in hidden tabs): the held input releases to neutral within about 250 ms and the pad stays plugged in | |
| 14 | Watchdog resumes | Bring the tab back, press a button: input resumes without reconnecting | |
| 15 | Takeover | Open a second tab/client: the first is closed with code 4001, second drives the pad | |
| 16 | Stuck button after kill | Hold a button, end the browser process in Task Manager: input releases (watchdog or close) | |
| 17 | Rumble | In a game (or tester) that vibrates the pad: the controller rumbles, the ghostpad page shows L/S values, `/stats` `rumble_sent` rises, and vibration stops when the game stops it. Run with `--trace` to see `rumble large= small=` lines | |
| 18 | Daemon killed | Hold a button, end `python`/`ghostpad.exe` in Task Manager: the virtual pad disappears from `joy.cpl` (driver cleanup; the daemon cannot release on a hard kill) | |
| 19 | Daemon Ctrl+Break | Hold a button, press Ctrl+Break in the daemon console: pad neutral, then unplugged; page shows disconnected | |

Note for 13: the watchdog is fed only by STATE and PAD_GONE frames; PINGs and WebSocket
ping/pong do not count, so a stalled poll loop cannot be masked. Measured with the mock
backend on this Windows 11 machine (100 trials, last STATE to neutral): median 203.0 ms,
max 215.5 ms.

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

Option A, packaged exe plus Task Scheduler: see `daemon/packaging/README.md`
(`build_exe.py`, `install_autostart.ps1`).

Option B, no exe, Startup folder: press `Win+R`, run `shell:startup`, and create
`ghostpad.cmd` there:

```
@echo off
cd /d C:\path\to\ghostpad\daemon
start "" /min pythonw -m ghostpad --token SECRET
```

`pythonw` has no console, so there is no log output and errors are invisible; use
`python` with `start /min` while debugging. Startup-folder items run at user logon only.
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

- Anything with the ViGEmBus driver installed: the checklist in section 4.
- Whether the newest upstream ViGEmBus works with `vgamepad`'s bundled ViGEmClient
  (`vgamepad` only warns on a version mismatch).
- The Startup-folder option (7B) and `pythonw`.
