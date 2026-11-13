# Latency testing

Tools for the budget in `DESIGN.md` (median added latency <= 5 ms, p99 <= 16.7 ms).
Do not quote a latency figure without numbers from one of these.

| Tool | Measures | Excludes |
|---|---|---|
| Web HUD (`#latency`) | PING RTT browser to daemon, client poll cadence, STATE rate | Bluetooth, game, display |
| `GET /stats` on the daemon | Frame receipt to backend apply, counters | Everything before receipt and after apply |
| `bench/bench.py` | Same as the HUD and `/stats`, from a synthetic client | Browser, polling quantisation, Bluetooth, display |
| Button-to-photon (240 fps camera) | Everything, end to end | Nothing; coarse (4.2 ms resolution) |

## a. Reading the HUD

Open the web client (`http://localhost:47800/` on the PC, or the Tizen app). The
latency panel fills in a few seconds after connecting (PING every 250 ms).

| Item | Meaning |
|---|---|
| RTT | `now - t_client` on the PONG, in ms |
| Net RTT | RTT minus the daemon's `proc_us` (time between receiving the PING and sending the PONG) |
| One-way | Net RTT / 2. Assumes a symmetric path; it is an estimate, not a measurement |
| min / median / p95 / p99 / max | Nearest-rank percentiles over the last 240 PONGs (about 60 s) |
| Jitter | Mean absolute difference of consecutive RTTs |
| Lost | PINGs with no PONG within 1 s, over PINGs resolved so far |
| Server proc | Median `proc_us`, in microseconds |
| Poll median / p99 | Actual interval between `getGamepads()` polls. About 16.7 ms on rAF at 60 Hz, about 4 ms with the fast timer |
| STATE rate | STATE packets sent in the last second (changes plus heartbeats) |

Colours: the headline pair is graded on **one-way** time.

| | Green (OK) | Amber (SLOW) | Red (OVER) |
|---|---|---|---|
| Median | <= 5 ms | <= 10 ms | > 10 ms |
| p99 | <= 16.7 ms | <= 33.4 ms | > 33.4 ms |

The text carries the verdict too, so it does not depend on colour. Buttons:
**Reset** clears the windows; **Copy stats as JSON** copies a snapshot (falls back to a
selected text box if the browser blocks the clipboard); **Polling** switches between
`raf` and `fast` (also `?poll=raf|fast`, default `raf`). Switching clears the poll stats.

Caveat: the HUD measures the network path to the daemon only. It does not include the
controller's Bluetooth link, the wait for the next poll, ViGEm, the game or the display.
A green HUD does not mean the added latency is within budget; that needs button-to-photon.

PINGs are only sent while the poll loop is ticking. The daemon watchdog counts any
frame as liveness, so a stalled poll loop must not be hidden behind PINGs.

## b. bench.py

Stdlib only. Sends STATE at `--rate` with values that change every frame, plus PINGs, and
prints RTT, network RTT, jitter, loss, the generator's own send-interval accuracy and the
daemon's `/stats`.

Loopback, no setup (starts a mock daemon on a free port and stops it afterwards):

```
python bench/bench.py --spawn --rate 250 --duration 10 --ping-interval-ms 20
python bench/bench.py --spawn --json > bench/results/loopback.json     # bench/results/ is gitignored
```

`--ping-interval-ms` defaults to 250, matching the HUD, which gives only 40 samples in
10 s (p99 equals max). Use 20 ms for meaningful percentiles.

Over the LAN, from another machine:

1. On the PC: `python -m ghostpad --token SECRET` (default bind `0.0.0.0`, port 47800).
   Allow the port through Windows Firewall for the private network.
2. Wired Ethernet on both ends for the headline number; repeat on Wi-Fi to see jitter.
3. On the other machine: `python bench/bench.py --host <pc-ip> --token SECRET --rate 250 --duration 60 --ping-interval-ms 20`.
4. A real controller client takes over the pads (close 4001). Do not run bench while
   playing. Against the vigem backend, bench drives pad 0 with garbage input, so close games first.

Reading the output:

| Line | Meaning |
|---|---|
| RTT / net RTT / one-way~ | As in the HUD |
| proc | Daemon PING to PONG time, microseconds |
| Generator | Achieved STATE rate and send-interval spread. If this is far from the target, the client machine is the bottleneck and the RTT numbers are suspect |
| recv->apply | Daemon-side time from frame receipt to backend apply, microseconds |
| frames, stale, malformed, watchdog_trips | Daemon counters; a non-zero `stale` or `watchdog_trips` during a steady run means loss or reordering |

**Loopback numbers exclude Bluetooth, polling quantisation, the real LAN or Wi-Fi and
display lag.** They show the daemon and protocol overhead only, as a floor.

The daemon also serves `GET /stats` (JSON) at any time, e.g.
`curl http://<pc-ip>:47800/stats`, and logs the same snapshot on exit. It is
unauthenticated like the static files.

## c. Button-to-photon with a 240 fps phone camera

Goal: the end-to-end added latency of ghostpad versus the controller plugged into the PC.

### Setup

- Phone with 240 fps slow motion (iPhone: Slo-mo 1080p at 240; many Android phones have it).
  Fix it on a tripod or a stack of books; no hand-holding.
- A test target on the **PC** that reacts visibly to a button the instant it is read. Use
  an existing tool rather than building one: a gamepad tester web page (any page that
  lights up a button, run in a second browser window, not the ghostpad one), or a game
  menu that highlights on press. The same target and scene for every setup.
- Frame the controller button and the TV screen together in one shot. A strong light or
  a lit, high-contrast target region helps frame counting.
- TV in Game Mode (and PC at the same refresh rate, vsync setting and resolution in every setup).
- Disable anything that adds frames to one setup only (frame interpolation, extra windows
  on top of the target, power-saving).

### Procedure

1. Start recording at 240 fps.
2. Press the button firmly and release, about 1 s apart. Do at least **20 presses** per
   setup; 30 is better. Do not time-align them to anything.
3. Play back frame by frame. Count frames from the **button bottoming out** (the first
   frame where the cap stops moving, or the micro-switch click is visible) to the **first
   frame where the screen changes**. Frames / 240 x 1000 = ms.
4. Record each press, then the median, p95 and max per setup.

Setups to compare, same controller, same scene:

| # | Setup | What it includes |
|---|---|---|
| 1 | Controller wired (or Bluetooth) directly to the PC | Baseline: controller link, OS, game, display |
| 2 | ghostpad via the browser on the PC (`http://localhost:47800/`, controller on the PC) | Baseline plus browser polling, WebSocket, daemon, ViGEm |
| 3 | ghostpad via the TV app (controller paired to the TV, PC on the LAN) | The real product: adds Bluetooth to the TV, LAN, TV polling |

Added latency of ghostpad = setup 2 minus setup 1 (software only) and setup 3 minus
setup 1 (everything). Setup 2 vs 1 isolates browser polling plus the daemon; 3 vs 2
isolates the TV and LAN. If the controller is Bluetooth in setup 1 but wired in 2, say so:
keep the same physical connection where possible.

For setup 2, repeat with `?poll=fast` and compare against `raf`.

### Results table template

Date / hardware / TV model and mode / camera fps / target used:

| Setup | Presses | Frames (all presses, comma separated) | Median ms | p95 ms | Max ms | Notes |
|---|---|---|---|---|---|---|
| 1 Direct to PC | 20+ | | | | | |
| 2 ghostpad, browser on PC, raf | 20+ | | | | | |
| 2b ghostpad, browser on PC, fast | 20+ | | | | | |
| 3 ghostpad, TV app | 20+ | | | | | |

Verdict against budget: median added (3 - 1) = ___ ms (target <= 5), p99 added = ___ ms (target <= 16.7).

### Caveats

- 240 fps means **4.2 ms resolution** per frame and +-1 frame of error in picking the
  start and end frames. Differences under about 4 ms are not resolvable with one setup of
  20 presses; use the medians over many presses and report the spread.
- With p99 and 20 presses, the "p99" is just the maximum. Use 50+ presses if the tail matters.
- The screen refresh quantises the result: at 60 Hz, a change shows up within a 16.7 ms
  window regardless of when it was applied. Compare setups with the same refresh rate.
- TV Game Mode must be on for all setups. Check it did not switch off when the input changed.
- Same game or test page, same scene, same window state, in every setup.
- Button travel is not zero: pick one consistent definition of "pressed" (bottomed out)
  and stick to it.
- Thermal, background load and Wi-Fi congestion vary: record them in the notes column.
- This is the only measurement that includes Bluetooth, the game and the display. It
  needs real hardware and has **not been done yet** (see `PLAN.md`, M3).
