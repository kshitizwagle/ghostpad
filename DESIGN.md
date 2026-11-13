# ghostpad — Design

Source of truth for architecture and the wire protocol. Change this doc and the code
together. Product scope lives in `PRODUCT.md`, work breakdown in `PLAN.md`.

## Architecture

```
controller ──BT/USB──▶ client (TV / browser)
                        │  Gamepad API, polled on rAF (or a 4 ms timer)
                        │  STATE on change + 50 ms heartbeat
                        ▼
                 ws://<pc-ip>:47800/ws  (LAN, WebSocket, binary + JSON text)
                        │
                        ▼
              ghostpad daemon (PC)  ◀── HTTP GET / serves client/web/ (same port)
                ┌───────────────────────────────┐
                │ ws server → decode → watchdog │  no input for 200 ms → pads neutral
                │            → PadBackend       │  close → neutral, then unplug
                └──────────────┬────────────────┘
                               │ vigem (Windows) | mock | uinput (later)
                               ▼
                       virtual Xbox 360 pad
                               │
TV  ◀─────────────── HDMI (video + audio) ───── PC game reads the pad
```

Input only. Video and audio never touch the daemon.

## Repo layout

| Path | Contents |
|---|---|
| `daemon/ghostpad/` | Python package: `server.py`, `wsproto.py`, `protocol.py`, `stats.py`, `backends/`, `__main__.py` (CLI) |
| `daemon/tests/` | Unit tests (`unittest`), runnable without Windows |
| `client/web/` | Browser test client: static, no build step, served by the daemon; JS tests in `client/web/tests/` |
| `client/tizen/` | Tizen TV app |
| `client/tizen/spike/` | Tizen feasibility spike (M4): minimal app, see `docs/tizen-spike.md` |
| `bench/` | Latency scripts (synthetic client, analysis) and their tests; `vigem_check.py` (Windows end-to-end check through XInput) |
| `docs/` | Procedures, e.g. `latency-testing.md` |

## Daemon

- Python 3.11+, `asyncio`.
- **Stdlib only for networking.** A minimal hand-written RFC 6455 WebSocket server.
  No `websockets` dependency.
- Only runtime dependency: `vgamepad` (Windows, needs the ViGEmBus driver), imported
  lazily so the daemon runs and tests on any OS with the `mock` backend.
- Single port, default `47800`:
  - plain HTTP `GET` serves the static web client from `client/web/` (override with `--web-root`); `GET /stats` returns JSON counters
  - WebSocket upgrade at path `/ws`
- Bind address configurable, default `0.0.0.0`.

### Modules

| Module | Responsibility |
|---|---|
| `wsproto.py` | RFC 6455 handshake, framing, masking, fragmentation, ping/pong/close handling |
| `server.py` | asyncio listener: static HTTP GET, `/stats`, `/ws` upgrade; per-connection `Client` state, pad ownership, seq filtering, hello timeout, watchdog, tracing |
| `protocol.py` | Binary encode/decode of messages, JSON hello/welcome |
| `backends/` | `PadBackend` ABC, `vigem`, `mock` |
| `stats.py` | recv→apply histogram and counters; `GET /stats`, periodic and exit summary |
| `__main__.py` | CLI |
| `tray.py` | Windows tray app (`--tray`): server on a worker-thread loop, Win32 icon and menu via ctypes on the main thread |
| `trayicon.py` | Tray and exe icon artwork drawn in code (signed distance functions), PNG/ICO encoding |

### CLI

```
python -m ghostpad [--port 47800] [--bind 0.0.0.0] [--backend auto|vigem|mock]
                   [--token SECRET] [--web-root DIR] [--verbose] [--trace]
                   [--tray] [--log-file PATH]
```

| Flag | Meaning |
|---|---|
| `--backend` | `auto` (default): `vigem` on Windows, `mock` elsewhere. On Windows `auto` falls back to `mock` with a warning naming the cause if `vgamepad` is missing or cannot reach ViGEmBus. `vigem` / `mock` force one; `vigem` exits with code 2 when unavailable |
| `--web-root` | Directory served for static files; default `client/web/` |
| `--token` | See Security |
| `--verbose` | DEBUG logging and a one-line stats summary every 30 s |
| `--trace` | Log every pad connect / update / neutral / unplug and PAD_GONE at DEBUG (implies DEBUG level). Per-frame output, noisy; off by default and costs nothing on the hot path (a single boolean check, no string formatting) |
| `--tray` | Windows only: notification-area icon (state colour, status, Open log, Open web test client, Start with Windows, Quit). Logs to a rotating file, default `%LOCALAPPDATA%\ghostpad\ghostpad.log`. Quit, logoff/shutdown (`WM_ENDSESSION`) and Ctrl+C stop the server normally (neutral, then unplug). Second instance on the same port: refused (named mutex) |
| `--log-file` | Also log to this file (rotated at 1 MB, 3 kept) |

### PadBackend

```python
class PadBackend(Protocol):
    name: str                                   # "vigem" | "mock"
    max_pads: int                               # 4
    def connect(self, pad: int) -> None: ...    # plug in virtual pad
    def update(self, pad: int, state: PadState) -> None: ...
    def neutral(self, pad: int) -> None: ...    # release everything
    def disconnect(self, pad: int) -> None: ... # unplug
    def close(self) -> None: ...
    # h(pad, large, small) on force feedback from the game; may be called from any thread
    def set_rumble_handler(self, h: Callable[[int, int, int], None] | None) -> None: ...
```

| Backend | Platform | Notes |
|---|---|---|
| `vigem` | Windows | `vgamepad.VX360Gamepad`; needs ViGEmBus; imported lazily. `vgamepad` 0.2.0 connects to the bus at import, so a missing driver raises `RuntimeError` there; mapped to `BackendUnavailable`. Rumble via `register_notification` (callback on a ViGEmClient thread). Unplug happens in `VX360Gamepad.__del__`, so the callback must not reference the gamepad (a cycle would defer unplug to the GC) |
| `mock` | Any | Records every call and the last state per pad; used by tests and non-Windows runs. `emit_rumble()` test hook |
| `uinput` | Linux | Later (M6) |

`PadState` = `buttons: int, lt: int, rt: int, lx: int, ly: int, rx: int, ry: int`.
A pad is plugged in lazily on its first STATE.

## Protocol v1

- Transport: WebSocket at `/ws`.
- Binary frames, **little-endian**. First byte is the message type.
- Text frames are JSON control messages.

### Binary messages

| Type | Name | Dir | Size | Layout |
|---|---|---|---|---|
| `0x01` | STATE | C→S | 20 | `type u8, pad u8, seq u16, buttons u16, lt u8, rt u8, lx i16, ly i16, rx i16, ry i16, t_client u32` |
| `0x02` | PING | C→S | 13 | `type u8, id u32, t_client f64` |
| `0x03` | PONG | S→C | 17 | `type u8, id u32, t_client f64, proc_us u32` |
| `0x04` | PAD_GONE | C→S | 2 | `type u8, pad u8` |
| `0x05` | RUMBLE | S→C | 4 | `type u8, pad u8, large u8, small u8` |

Field notes:

- `pad`: 0-3. Anything else is dropped.
- `seq`: per-pad counter, increments per STATE sent, wraps at 65536.
- `buttons`: XInput `wButtons` bitmask (table below).
- `lt`, `rt`: 0-255.
- `lx, ly, rx, ry`: signed 16-bit, XInput convention, **+y is up**.
- `t_client`: ms on the client's monotonic clock, `u32`, wraps. Diagnostic only; the
  server never compares it with its own clock.
- PING `t_client`: ms as `f64` (e.g. `performance.now()`), echoed verbatim in PONG.
- PONG `proc_us`: server time between receiving the PING and sending the PONG, in
  microseconds. The client subtracts it from RTT.
- Malformed or unknown-type binary frames are dropped and counted, never fatal.

### Rumble

- RUMBLE carries the motor levels a game set on a virtual pad: `large` = low-frequency
  (left) motor, `small` = high-frequency (right), 0-255. Verified with ViGEmBus: a game's
  `XInputSetState(L, R)` (0-65535) arrives as `large = L >> 8`, `small = R >> 8`.
- Sent to the owning client only, for pads it has plugged, **on change only** per pad.
  Backend callbacks arrive on a driver thread and are moved onto the event loop
  (`call_soon_threadsafe`); the send does not wait for the socket buffer.
- The server sends 0/0 for a pad whose last RUMBLE was non-zero when the watchdog
  neutralises it, on PAD_GONE, and on release (close, takeover: before the 4001 close
  frame, best effort). While the watchdog holds a client's pads neutral, RUMBLE is not
  sent; changes after its next STATE are.
- The client plays a `dual-rumble` effect and re-issues it every 150 ms (200 ms effect)
  while the last levels are non-zero; it stops on 0/0 and when the connection drops
  (`client/web/rumble.js`).
- Counted in `/stats` as `rumble_sent`. Same wire format as before, so the version stays 1.

### JSON control messages (text frames)

Client first message:

```json
{"type":"hello","version":1,"client":"<name>"}
```

Server reply:

```json
{"type":"welcome","version":1,"backend":"vigem|mock","max_pads":4}
```

- Version mismatch: server closes with code **4000**.
- Binary frames before `hello` are dropped.
- A connection that completes the upgrade but sends no valid `hello` within **5 s** is closed with code **4003**. The deadline is absolute; junk frames do not extend it. Server parameter `hello_timeout` (default 5 s).

Client diagnostic line (optional, after `hello`; for clients without a usable console, e.g. the TV):

```json
{"type":"log","msg":"<text>"}
```

- Logged at INFO by the `ghostpad.client` logger as `<client name>: <text>`.
- `msg` is truncated to 500 chars; non-printable chars (newlines included) are escaped.
- Rate limit per connection: burst 50, then 20 lines/s. Excess lines are dropped and
  counted; the count is logged as a WARNING before the next accepted line.
- Other text after `hello` is ignored. Additive: v1 daemons before this ignore it too, so
  the version is unchanged.

### Close codes

| Code | Meaning |
|---|---|
| 1000 | Normal close |
| 4000 | Protocol version mismatch |
| 4001 | Taken over by a newer client |
| 4002 | Bad or missing token. Always sent after the 101 upgrade, never as an HTTP error |
| 4003 | No valid `hello` within 5 s of the upgrade |

### Sequence handling

`seq` is compared wrap-aware (serial number arithmetic): a frame is newer if
`(seq - last) mod 65536` is in `1..32767`. Equal or older frames are dropped without
touching the backend. The first frame for a pad is always accepted. Heartbeats carry a
fresh `seq`, so they are accepted like any other STATE.

## Input mapping

Browser Gamepad "standard" mapping to XInput.

| Gamepad index | Control | XInput | Value |
|---|---|---|---|
| buttons[0] | A | `A` | `0x1000` |
| buttons[1] | B | `B` | `0x2000` |
| buttons[2] | X | `X` | `0x4000` |
| buttons[3] | Y | `Y` | `0x8000` |
| buttons[4] | LB | `LEFT_SHOULDER` | `0x0100` |
| buttons[5] | RB | `RIGHT_SHOULDER` | `0x0200` |
| buttons[6] | LT | `lt` analog | `round(value*255)` |
| buttons[7] | RT | `rt` analog | `round(value*255)` |
| buttons[8] | Back | `BACK` | `0x0020` |
| buttons[9] | Start | `START` | `0x0010` |
| buttons[10] | LS click | `LEFT_THUMB` | `0x0040` |
| buttons[11] | RS click | `RIGHT_THUMB` | `0x0080` |
| buttons[12] | D-pad up | `DPAD_UP` | `0x0001` |
| buttons[13] | D-pad down | `DPAD_DOWN` | `0x0002` |
| buttons[14] | D-pad left | `DPAD_LEFT` | `0x0004` |
| buttons[15] | D-pad right | `DPAD_RIGHT` | `0x0008` |
| buttons[16] | Guide | `GUIDE` | `0x0400` |
| axes[0] | LX | `lx` | `round(v*32767)` |
| axes[1] | LY | `ly` | `round(-v*32767)` (Y inverted) |
| axes[2] | RX | `rx` | `round(v*32767)` |
| axes[3] | RY | `ry` | `round(-v*32767)` (Y inverted) |

Axis values are clamped to [-1, 1] before scaling, so results stay within
`-32767..32767`. LT/RT bits are never set in `buttons`; triggers are analog only.
Controllers without `mapping === "standard"` are ignored by the v1 clients.

## Send policy

- Client polls `navigator.getGamepads()` on `requestAnimationFrame` by default; an
  optional `fast` mode polls on a 4 ms `setInterval` (`?poll=fast`, see Measurement design).
- STATE is sent when the mapped state changes from the last sent state.
- Heartbeat: a STATE every **50 ms** per connected pad even when unchanged. Polls are
  quantised to the scheduler tick, so a heartbeat is due once `elapsed >= 50 - 8 ms`
  (`client/web/sendpolicy.js`); a strict `elapsed >= 50` check slipped to 66.7 ms on 60 Hz rAF.
- `seq` increments per pad.
- On controller disconnect (`gamepaddisconnected`), the client sends PAD_GONE; the
  server unplugs that virtual pad.
- On (re)connect, the client sends `hello`, then a full STATE for each pad present.

## Ownership and fail-safe

- **One client owns the pads at a time.** A second client that sends a valid `hello` takes
  over: the old connection is closed with code 4001 and its pads are released. Connecting
  alone, or a failed hello (bad version, bad token, timeout), does not displace the owner. Simplest model for a single user.
- **Watchdog:** based on **input frames only**, STATE and PAD_GONE. PING/PONG and
  WebSocket-level ping/pong do not count (PINGs are still answered). If the owning
  client has at least one plugged pad and **200 ms** pass without an input frame, all of
  its pads are set to neutral (buttons 0, triggers 0, sticks 0), once per stall (counted
  in `watchdog_trips`). Pads stay plugged in. The next STATE resumes normal operation.
  A client with no plugged pads is never tripped. This closes the gap where a client with
  a stalled poll loop kept the link alive with PINGs while a button stayed held.
- **Close:** on connection close, pads go neutral, then are unplugged.
- **Shutdown:** the daemon neutralises and unplugs all pads on exit (SIGINT, SIGTERM; on
  Windows Ctrl+C and Ctrl+Break). A hard kill (Task Manager, `Stop-ScheduledTask`) skips
  this; pads are then left to the driver's cleanup when the process handle closes. Measured
  with ViGEmBus 1.17.333: the pad left XInput 2.1-2.2 ms after `TerminateProcess` (3 runs).
- Watchdog tick is 10 ms nominal, so worst-case release is about 220 ms. On Windows the
  event loop's timers run at the ~15.6 ms system tick; measured there (Windows 11, Python
  3.12, mock backend, 100 trials, last STATE to neutral): median 203.0 ms, max 215.5 ms.
  This meets the 250 ms target in `PRODUCT.md`.
- A half-open TCP link (cable pulled) is covered by the watchdog, not by TCP timeouts.

## Security

- LAN only, no TLS, **no auth in v1**.
- Optional `--token SECRET`: clients pass it as `?token=SECRET` on the `/ws` URL and on
  `GET /stats` (HTTP 403 otherwise). Compared in constant time. A bad `/ws` token is
  always 101 then close 4002. Static files stay open. Without a token, anyone on the LAN can drive the pad.
- Bind address configurable (`--bind`, default `0.0.0.0`). Use `127.0.0.1` for local
  testing.
- Static serving is restricted to the `client/web/` tree; paths with `..` or absolute
  paths are rejected.
- Do not expose the port to the internet.

## Latency budget

Targets on wired LAN, compared with the controller plugged directly into the PC:

| Metric | Target |
|---|---|
| Median added latency | ≤ 5 ms |
| p99 added latency | ≤ 16.7 ms (one 60 Hz frame) |
| All-neutral after link loss | ≤ 250 ms |

Sources of delay:

| Source | Expected | Ours? | Notes |
|---|---|---|---|
| Controller → TV (Bluetooth) | Several ms | No | Same class of delay as a PC-paired controller; excluded from the "added" figure only if measured as a baseline |
| rAF polling quantisation | 0 to 16.7 ms at 60 Hz | Yes | **Biggest known contributor.** Mitigation idea: poll via `setInterval` or a `MessageChannel` loop faster than rAF. To be tested, not assumed |
| Network RTT / 2 | < 1 ms wired | Yes | Wi-Fi on either end adds jitter |
| Daemon processing | Sub-millisecond | Yes | Decode, seq check, backend call |
| ViGEm update | Sub-millisecond | Partly | Driver report submission. Measured on loopback, STATE sent to XInput-visible (WebSocket + daemon + ViGEm + XInput read): median 0.100-0.225 ms, p99 0.189-0.528 ms, 3 runs of 200-500 samples (`bench/vigem_check.py`) |
| Game's own polling | 0 to 1 frame | No | Same with a directly attached pad |

## Measurement design

Built in M3. Procedures and how to read the numbers: `docs/latency-testing.md`.

- **PING every 250 ms** from the web client HUD (`client/web/latency.js`, DOM-free).
  RTT is `now - t_client` from the echoed PONG; network RTT is `RTT - proc_us/1000`; one-way
  is estimated as network RTT / 2. Rolling window of the last 240 samples (about 60 s):
  min / median / p95 / p99 / max (nearest rank: `sorted[min(n-1, floor(p*n))]`), jitter
  (mean absolute difference of consecutive RTTs), lost pings (no PONG within 1 s), server
  proc median. PINGs do not count as watchdog liveness (input frames only), so a stalled poll loop is
  neutralised regardless of PINGs.
- **HUD polling stats:** actual interval between `getGamepads()` polls (median / p99) and
  STATE packets sent in the last second. Colour grading uses one-way time: median
  <= 5 ms green, <= 10 ms amber, else red; p99 <= 16.7 ms green, <= 33.4 ms amber, else red.
  A "Copy stats as JSON" button exports the snapshot.
- **Daemon stats (`stats.py`).** Counters are plain integer increments on the hot path;
  the recv->apply histogram is a bounded deque of the last 200 000 samples, sorted only
  when a snapshot is requested (copied on the loop thread, sorted in a worker).
  - `GET /stats` returns JSON (also `HEAD`; requires `?token=` when `--token` is set, otherwise open):

    ```json
    {"recv_apply":{"count":2501,"window":2501,"min_us":4.3,"median_us":10.4,"p95_us":18.2,
                   "p99_us":30.4,"max_us":304.2},
     "frames_received":3000,"stale_drops":0,"malformed_drops":0,"prehello_drops":0,
     "watchdog_trips":0,"ping_count":499,"rumble_sent":0,"uptime_s":10.1}
    ```

  - `recv_apply.count` is all-time STATE frames applied; `window` is the samples behind the
    percentiles. Percentile fields are `null` until the first sample.
  - `frames_received`: binary frames from an active (post-hello) client. `stale_drops`:
    seq filter. `malformed_drops`: undecodable, unknown-type or bad-pad frames.
    `prehello_drops`: binary before hello. `watchdog_trips`: 200 ms input-silence neutralisations.
    `rumble_sent`: RUMBLE messages sent.
  - Logged as `exit stats: {json}` on shutdown, and as a one-line summary every 30 s with
    `--verbose`. Logs go to `logs/` (gitignored) when redirected there.
- **`bench/bench.py` synthetic client** (stdlib only, own minimal WebSocket client).
  STATE at `--rate` with changing values plus PINGs; prints RTT / network RTT / jitter / loss,
  the generator's send-interval accuracy and the daemon `/stats`; `--json` for machines;
  `--spawn` starts a local mock daemon for loopback runs. Loopback excludes Bluetooth,
  polling quantisation, real LAN/Wi-Fi and display lag. On Windows it uses the selector
  event loop and a 1 ms timer resolution (`timeBeginPeriod`): with the default proactor
  loop and an open socket, 4 ms sends came out in bursts (send interval p95 ~30 ms); with
  both, median 4.0 ms, p95 5.0 ms.
- **Polling experiment.** `client/web/schedulers.js` offers `raf` (default) and `fast`
  (`setInterval` 4 ms), selected with `?poll=raf|fast` or the HUD toggle. In headless
  Chrome with a fake `getGamepads()`, `fast` polled at a 4.0 ms median (p99 4.6-5.0 ms)
  and cut the change-to-send delay from median 7.7-8.9 ms / p99 16.1 ms (raf) to median
  1.9-2.0 ms / p99 4.0 ms (2 runs each, 300 changes per run). This measures the scheduler only; whether Chrome refreshes the real
  gamepad state that often is unmeasured (needs hardware). Default stays `raf` until then.
- **`bench/vigem_check.py` (Windows, needs ViGEmBus).** Starts the daemon with the vigem
  backend, drives it with a protocol client and reads the pads back with XInput: mapping,
  watchdog, unplug paths, rumble (as the game, via `XInputSetState`), four pads, Ctrl+Break,
  hard kill, and STATE-to-XInput latency. `--exe` runs the packaged build instead.
- **Button-to-photon.** Film the controller and the screen with a phone at 240 fps
  slow-motion; count frames from button press to on-screen change. Compare controller on
  PC, ghostpad via browser on PC, ghostpad via the TV app, over 20+ presses each. Procedure
  and results template in `docs/latency-testing.md`. **Not yet done** (needs hardware).
- No latency claim is made in docs or commits without numbers from these tools.

## Known risks and open questions

| Risk | Detail |
|---|---|
| `tizen.tvwindow` availability | API is "Optional" on TVs; unverified on the 2025 U8500F. Privilege `http://tizen.org/privilege/tv.window` is public. Resolved by M4 |
| Game Mode and audio | Unknown whether low-latency Game Mode and HDMI audio survive when HDMI is shown inside the app window |
| Background pause | Tizen pauses JS in background apps, so the app must stay foreground; this is why HDMI is shown inside the app |
| Secure context | Gamepad API in desktop Chrome needs a secure context. `localhost` works, plain `http://LAN-IP` does not. Affects the browser test client from another machine, not packaged Tizen apps |
| ViGEmBus | Unmaintained, but works: 1.17.333 (bundled with `vgamepad` 0.2.0) verified on Windows 11 |
| rAF quantisation | May dominate the latency budget; see mitigation above |
