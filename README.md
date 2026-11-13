# ghostpad

Play PC games on the TV over a direct HDMI cable while the controller stays paired to
the TV. ghostpad forwards controller input over the LAN (WebSocket) to a virtual Xbox
360 pad on a Windows PC, via ViGEmBus. Input only: video and audio stay on the HDMI
cable, so there is no encode/decode step and no streaming stack.

```
controller ──BT/USB──▶ client (TV / browser)  ──WebSocket/LAN──▶  ghostpad daemon (PC)
                                                                         │
TV  ◀──────────────────────── HDMI (video + audio) ──────────────────────┘ virtual X360 pad
```

## Status

| Milestone | State |
|---|---|
| M0 Bootstrap (docs) | Done |
| M1 Windows daemon | Done: tests pass on Linux and Windows 11; ViGEm backend verified through XInput (`bench/vigem_check.py`). A real controller in the browser and a real game not tried yet |
| M2 Browser test client | Written; headless-tested only |
| M3 Latency measurement | Tooling written; **no real-hardware numbers yet** |
| M4 Tizen spike | Written; **never run on a TV** (go/no-go still open) |
| M5 Tizen client | Not started |
| M6 Polish | In progress: rumble, exe, autostart and four pads verified on Windows 11; Linux backend open |

On Windows the daemon side is verified with ViGEmBus; the browser path with a real controller is not yet. Expect rough edges.

## Quick start (Windows 10/11)

1. Install Python 3.11+ and the ViGEmBus driver (project is archived but still works;
   see `docs/windows-setup.md` for where to get it).
2. `pip install vgamepad` (on Windows this also offers to install ViGEmBus 1.17.333 if no ViGEmBus is installed)
3. From the repo root:
   ```
   cd daemon
   python -m ghostpad
   ```
4. Open <http://localhost:47800> in Chrome or Edge, connect a controller, press a button.
5. Run `joy.cpl` (Game Controllers): an "Xbox 360 Controller" should appear and react.

The pad is plugged in lazily on the first button press, so it will not show before that.
`python -m ghostpad --tray` runs it with a notification-area icon instead of a console
(status, log, quit; the packaged tray exe can also start with Windows).
Full walkthrough, firewall rule, autostart and troubleshooting: `docs/windows-setup.md`.

## Trying it without Windows

```
cd daemon
python -m ghostpad --backend mock --verbose
```

The `mock` backend needs no driver and runs on any OS; it just records what a pad would
receive. Tests: `cd daemon && python -m unittest discover -s tests -v`.

## Browser client: secure-context caveat

Desktop Chrome only exposes the Gamepad API on secure contexts. `http://localhost`
works; `http://192.168.x.y:47800` from another machine does not (the page shows a
banner). The packaged Tizen app is not affected. Use the browser client on the PC itself,
or a workaround such as Chrome's `--unsafely-treat-insecure-origin-as-secure` flag for
test machines.

## CLI

```
python -m ghostpad [--port 47800] [--bind 0.0.0.0] [--backend auto|vigem|mock]
                   [--token SECRET] [--web-root DIR] [--verbose]
```

## Latency

Measure, do not guess. Tools: the web HUD, `GET /stats` on the daemon, `bench/bench.py`
and a 240 fps button-to-photon procedure. See `docs/latency-testing.md`. No latency
figures are claimed here because none have been measured on hardware yet.

## Tizen TV app

The M4 spike (HDMI shown inside the app via `tizen.tvwindow`) is in `client/tizen/spike/`
with the procedure in `docs/tizen-spike.md`. Not yet run on a TV.

## Security

LAN only. No TLS and, by default, no authentication: anyone who can reach the port can
drive the pad. Start with `--token SECRET` (clients pass `?token=SECRET`) and allow the
port only on a Private network profile. Never expose it to the internet. Details in
`DESIGN.md` § Security.

## Docs

| File | Contents |
|---|---|
| [`PRODUCT.md`](PRODUCT.md) | Scope, goals, non-goals, success criteria |
| [`DESIGN.md`](DESIGN.md) | Architecture, protocol v1, latency budget |
| [`PLAN.md`](PLAN.md) | Milestones and checklists |
| [`docs/windows-setup.md`](docs/windows-setup.md) | First run on Windows, verification checklist |
| [`daemon/packaging/`](daemon/packaging/) | PyInstaller exe (console or tray app) and autostart scripts |

## License

No license has been chosen yet. Until one is added, all rights are reserved by default.
