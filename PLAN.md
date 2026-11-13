# ghostpad — Plan

## Summary

ghostpad forwards controller input over the LAN to a virtual Xbox 360 pad on a
Windows PC that is wired to a Samsung Tizen TV over HDMI. The controller stays paired
to the TV; video and audio stay on the cable. Why: avoid streaming latency and colour
issues while keeping the couch controller. See `PRODUCT.md` for scope and
`DESIGN.md` for architecture and protocol.

Status: **M0 in progress.**

## Milestones

| ID | Name | Outcome | Status |
|---|---|---|---|
| M0 | Bootstrap | Repo docs (PRODUCT, DESIGN, PLAN, CLAUDE) | Done |
| M1 | Windows daemon | WS server, protocol decode, PadBackend (vigem + mock), watchdog, CLI, tests without Windows | Done |
| M2 | Browser test client | Static page: Gamepad API to STATE, connection status, live pad view | Not started |
| M3 | Latency measurement | PING/PONG, HUD stats, daemon histograms, bench script, button-to-photon doc | Not started |
| M4 | Tizen spike (go/no-go) | Sideloadable app showing HDMI via tvwindow with gamepad overlay; feasibility verdict | Not started |
| M5 | Tizen client | Real TV app: config, auto-reconnect, HDMI source select, overlay | Not started |
| M6 | Polish | Rumble, PyInstaller exe + autostart, Linux uinput backend, install guide | Not started |

M4 can start in parallel with M2/M3 since it is the biggest unknown. If M4 is a
no-go, M5 is replanned before any further Tizen work.

## M0 — Bootstrap

- [ ] `PRODUCT.md` merged
- [ ] `.gitignore` covers Python, Tizen signing material, logs
- [ ] `DESIGN.md` documents architecture, protocol v1, latency budget
- [ ] `PLAN.md` (this file)
- [ ] `CLAUDE.md` for AI agents
- [ ] `README.md` stub pointing at the docs

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

- [ ] PING every 250 ms; PONG handling with `proc_us`
- [ ] HUD: RTT min/median/p95/p99 and jitter
- [ ] Daemon recv→apply histogram, periodic and on-exit log summary
- [ ] `bench/` synthetic client (loopback and LAN, configurable rate)
- [ ] `docs/latency-testing.md`: 240 fps phone button-to-photon procedure
- [ ] Baseline run: controller on PC vs ghostpad, with numbers recorded
- [ ] Test `setInterval`/`MessageChannel` polling against rAF

Exit criteria: documented numbers show whether median ≤ 5 ms and p99 ≤ 16.7 ms hold on
wired LAN, with the polling-mitigation result recorded.

## M4 — Tizen spike (go/no-go)

- [ ] Minimal sideloadable app (`client/tizen/`), developer mode on the U8500F
- [ ] Check `tizen.tvwindow` presence and the `tv.window` privilege at runtime
- [ ] Show HDMI input inside the app window
- [ ] Gamepad readout overlay; confirm input is still delivered with HDMI shown
- [ ] Verify HDMI audio keeps playing
- [ ] Check Game Mode / low-latency behaviour and visible picture lag
- [ ] Verify the app stays foreground and JS is not paused
- [ ] Write findings and a go/no-go verdict in `docs/`

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

- [ ] RUMBLE: vigem notification callback to message 0x05; client uses `vibrationActuator`
- [ ] PyInstaller single exe for Windows
- [ ] Autostart on Windows login
- [ ] Linux `uinput` backend
- [ ] Multi-pad (up to 4) verified end to end
- [ ] `README.md` install guide (ViGEmBus, daemon, TV sideload)

Exit criteria: a clean-machine install following only the README works end to end.

## Working agreements

- **Bugs:** found during development are logged as GitHub issues. Minor ones that
  don't affect main functionality or usability are deferred; blockers are fixed
  immediately.
- **Branches:** each milestone lands via its own branch and PR.
- **Docs and code move together:** protocol changes update `DESIGN.md` in the same PR.
- **Checklists:** tick the boxes in this file as tasks complete.
- **Latency claims** must be backed by measurements from the M3 tooling.
- **No secrets:** Tizen signing material is never committed.
