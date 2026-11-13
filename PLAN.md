# ghostpad — Plan

## Summary

ghostpad forwards controller input over the LAN to a virtual Xbox 360 pad on a
Windows PC that is wired to a Samsung Tizen TV over HDMI. The controller stays paired
to the TV; video and audio stay on the cable. Why: avoid streaming latency and colour
issues while keeping the couch controller. See `PRODUCT.md` for scope and
`DESIGN.md` for architecture and protocol.

Status: **M1-M3 code complete; all test suites pass on Linux and Windows 11 (mock backend). ViGEm backend untested with the driver; M3 hardware measurements pending. M4 written, untested on TV. M6 in progress (rumble done; exe and autostart verified on Windows without the driver). M5 not started.** See "Needs hardware".

## Milestones

| ID | Name | Outcome | Status |
|---|---|---|---|
| M0 | Bootstrap | Repo docs (PRODUCT, DESIGN, PLAN, CLAUDE) | Done |
| M1 | Windows daemon | WS server, protocol decode, PadBackend (vigem + mock), watchdog, CLI, tests without Windows | Written; tests pass on Windows; ViGEm backend untested with the driver |
| M2 | Browser test client | Static page: Gamepad API to STATE, connection status, live pad view | Done |
| M3 | Latency measurement | PING/PONG, HUD stats, daemon histograms, bench script, button-to-photon doc | Tooling done; hardware measurements pending |
| M4 | Tizen spike (go/no-go) | Sideloadable app showing HDMI via tvwindow with gamepad overlay; feasibility verdict | Written; untested on TV |
| M5 | Tizen client | Real TV app: config, auto-reconnect, HDMI source select, overlay | Not started |
| M6 | Polish | Rumble, PyInstaller exe + autostart, Linux uinput backend, install guide | In progress |

M4 can start in parallel with M2/M3 since it is the biggest unknown. If M4 is a
no-go, M5 is replanned before any further Tizen work.

## M0 — Bootstrap

- [x] `PRODUCT.md` merged
- [x] `.gitignore` covers Python, Tizen signing material, logs
- [x] `DESIGN.md` documents architecture, protocol v1, latency budget
- [x] `PLAN.md` (this file)
- [x] `CLAUDE.md` for AI agents
- [x] `README.md` stub pointing at the docs

Exit criteria: all four docs are on `main` and consistent with each other.

## M1 — Windows daemon

- [x] Package skeleton `daemon/ghostpad/` and `daemon/tests/`
- [x] Hand-written RFC 6455 server (handshake, framing, masking, ping/close) on `asyncio`
- [x] HTTP GET static serving from `client/web/` and `/ws` upgrade on one port
- [x] `protocol.py`: encode/decode STATE, PING, PONG, PAD_GONE; JSON hello/welcome; close 4000
- [x] `PadBackend` ABC with `mock` and `vigem` (lazy `vgamepad` import)
- [x] Session logic: wrap-aware seq filter, ownership takeover (4001), `--token` check
- [x] Watchdog: 200 ms neutral, close then unplug, clean shutdown
- [x] CLI: `python -m ghostpad --port --bind --backend --token --verbose`

Exit criteria: unit tests pass on Linux and Windows with the `mock` backend; on
Windows a real game sees a virtual X360 pad driven by a hand-made client.

## M2 — Browser test client

- [x] `client/web/index.html` + plain ES modules, no build step
- [x] Gamepad "standard" mapping to STATE per `DESIGN.md`
- [x] Send policy: rAF polling, send on change, 50 ms heartbeat, per-pad `seq`
- [x] `hello`/`welcome` handshake, connection status, auto-reconnect
- [x] PAD_GONE on controller disconnect
- [x] Live visualisation of buttons, triggers and sticks
- [x] Config via URL (`?host=&token=`); document the secure-context limitation

Exit criteria: a controller connected to a Chromium browser on `localhost` drives a
virtual pad that shows up in a Windows game controller tester.

## M3 — Latency measurement

- [x] PING every 250 ms; PONG handling with `proc_us`
- [x] HUD: RTT min/median/p95/p99 and jitter
- [x] Daemon recv→apply histogram, periodic and on-exit log summary
- [x] `bench/` synthetic client (loopback and LAN, configurable rate)
- [x] `docs/latency-testing.md`: 240 fps phone button-to-photon procedure
- [ ] Baseline run: controller on PC vs ghostpad, with numbers recorded (button-to-photon, needs real hardware; not done)
- [x] Test `setInterval`/`MessageChannel` polling against rAF (headless, fake gamepad; real-hardware result still open)

Exit criteria: documented numbers show whether median ≤ 5 ms and p99 ≤ 16.7 ms hold on
wired LAN, with the polling-mitigation result recorded.

## M4 — Tizen spike (go/no-go)

- [x] Minimal sideloadable app (`client/tizen/spike/`; code only, developer mode on the U8500F not yet done)
- [x] Check `tizen.tvwindow` presence and the `tv.window` privilege at runtime (code; not run on TV)
- [x] Show HDMI input inside the app window (code; not run on TV)
- [x] Gamepad readout overlay (code); confirming input is delivered with HDMI shown needs the TV
- [ ] Verify HDMI audio keeps playing
- [ ] Check Game Mode / low-latency behaviour and visible picture lag
- [ ] Verify the app stays foreground and JS is not paused
- [ ] Write findings and a go/no-go verdict in `docs/` (`docs/tizen-spike.md` has the design and checklist; no verdict yet)

Exit criteria: a written verdict on each of API presence, input while HDMI shown,
audio, and Game Mode/lag. Failures are recorded as blockers with alternatives.

## M5 — Tizen client

- [ ] Port the M2 client logic into the Tizen app
- [ ] Config screen for PC IP, port and token; persisted locally
- [ ] Auto-reconnect with backoff
- [ ] HDMI source select
- [ ] Minimal overlay: connection state and optional RTT
- [ ] Sideload instructions in `docs/` (without any signing material)
- [ ] Soak test: a full real-game session over HDMI

Exit criteria: a full session with no dropped inputs or stuck buttons, and the
unplug-the-cable test releases all inputs within 250 ms.

## M6 — Polish

- [x] RUMBLE: vigem notification callback to message 0x05 (daemon, tested with the mock backend; with the driver: `docs/windows-setup.md` check 17); client uses `vibrationActuator` (`client/web/rumble.js`). Tizen vibration path is M5
- [x] PyInstaller exe for Windows (`daemon/packaging/build_exe.py`; one folder by default; verified on Windows 11 without the driver)
- [x] Autostart on Windows login (`daemon/packaging/install_autostart.ps1`, `uninstall_autostart.ps1`; install, start and uninstall verified on Windows 11; a real logon start not yet)
- [ ] Linux `uinput` backend
- [ ] Multi-pad (up to 4) verified end to end
- [x] `README.md` install guide (ViGEmBus, daemon, TV sideload) and `docs/windows-setup.md` (partly verified on Windows; driver steps not)

Exit criteria: a clean-machine install following only the README works end to end.

## Needs hardware

| Item | Where |
|---|---|
| Windows ViGEm verification: a real game sees the virtual X360 pad, rumble, release on daemon kill | issue #3, `docs/windows-setup.md` checks 1-19 |
| Tizen spike go/no-go on the TV (tvwindow, input with HDMI shown, audio, Game Mode) | issues #9, #10, `docs/tizen-spike.md` |
| Real-controller polling comparison (rAF vs fast timer) | issue #16 |
| Button-to-photon measurement | `docs/latency-testing.md` section c |

## Working agreements

- **Bugs:** found during development are logged as GitHub issues. Minor ones that
  don't affect main functionality or usability are deferred; blockers are fixed
  immediately.
- **Branches:** each milestone lands via its own branch and PR.
- **Docs and code move together:** protocol changes update `DESIGN.md` in the same PR.
- **Checklists:** tick the boxes in this file as tasks complete.
- **Latency claims** must be backed by measurements from the M3 tooling.
- **No secrets:** Tizen signing material is never committed.
