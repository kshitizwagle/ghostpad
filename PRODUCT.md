# ghostpad — Product

## One-liner

Play PC games on the TV over a direct HDMI cable, with the controller paired to the
TV instead of the PC. ghostpad forwards controller input over the LAN to a virtual
Xbox controller on the PC. It forwards only the input, never video or audio.

## Problem

- **Streaming:** Steam Link and Moonlight move video, audio *and* input over the
  network. Video streaming adds encode/decode latency and compression, and can get
  colour wrong. In practice that's a limited/full range mismatch or a colour profile
  that doesn't feel native.
- **Direct HDMI:** a direct cable from PC to TV gives the best picture and the lowest
  latency, but the controller then has to reach the PC: Bluetooth range, cable runs,
  dongle placement.
- **The TV:** the TV (Samsung, Tizen) already supports gamepads, but there's no way
  to send that input to a PC without a full streaming client.

## Solution

A tiny client on the device by the couch reads the controller and sends its state
over the network. A daemon on the PC presents it as a real Xbox 360 controller.
Video and audio stay on the HDMI cable.

```
controller ──BT/USB──▶ client (TV / browser)  ──WebSocket/LAN──▶  ghostpad daemon (PC)
                                                                         │
TV  ◀──────────────────────── HDMI (video + audio) ──────────────────────┘ virtual X360 pad
```

## Users

- **Primary:** a single home user with a gaming PC wired to the TV over HDMI, sitting
  on the couch with a Bluetooth controller paired to the TV.
- **Secondary:** anyone wanting to forward a controller from a browser-capable
  device to a PC (testing, a second room, a locked-down box).

## Goals

1. **Input only:** zero video encoding and no streaming stack.
2. **Latency:** added input latency small enough not to notice. Targets are in
   `DESIGN.md` § Latency budget.
3. **Measurable:** latency is measured and visible, not guessed. RTT, jitter and
   processing time are built in, and there's a repeatable button-to-photon test.
4. **Fail safe:** if the link drops, every button and axis is released within a
   bounded time. A button never stays held.
5. **Easy to run:** one command or exe on Windows, and a sideloaded app on the TV.

## Non-goals (for now)

- **No streaming:** no video or audio of any kind.
- **No internet play:** LAN only, no NAT traversal or relay.
- **No mouse/keyboard forwarding:** maybe later.
- **Not a Steam Input replacement:** Steam can remap on top of the virtual pad.
- **No store distribution:** the Tizen app is sideloaded.

## Platforms (in order)

| Order | Component | Platform |
|---|---|---|
| 1 | Daemon | Windows 10/11 (ViGEmBus via `vgamepad`) |
| 2 | Test client | Any Chromium browser with the Gamepad API |
| 3 | Real client | Samsung Tizen TV web app (2025 U8500F is the target device) |
| Later | Daemon | Linux (uinput) |
| Later | Client | Android (only if an unlocked box shows up) |

## Success criteria

- **Real play:** a full session of a real game played on HDMI with the controller
  paired to the TV, with no dropped inputs or stuck buttons.
- **Latency on wired LAN:** median added latency ≤ 5 ms and p99 ≤ 16.7 ms (one 60 Hz
  frame) compared with the controller plugged into the PC, measured by the bench in
  `PLAN.md` M3.
- **Release on disconnect:** pulling the network cable mid-game releases all inputs
  within 250 ms.

## Key risk

The Tizen app has to stay in the foreground to read the gamepad, because Tizen pauses
JS for background apps. The plan is to show the PC's HDMI input *inside* the app with
the TV Window API (`tizen.tvwindow`, public privilege). Whether that works on the
target TV, keeps HDMI audio, and stays out of extra picture processing (Game Mode) is
unverified. `PLAN.md` M4 is the go/no-go spike for exactly this.
