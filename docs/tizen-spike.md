# Tizen spike (M4): HDMI inside the app, go/no-go

Goal: decide whether a sideloaded Tizen web app on the Samsung UA43U8500FRSHE (2025)
can show the PC's HDMI input **inside the app** (so the app stays foreground and keeps
reading the gamepad) without losing HDMI audio or Game Mode.

**Status: code written, nothing has been run on a TV.** Only the TVWindow API page could be
read against primary docs (issue #10 pass, 2026-11-13); everything else marked UNVERIFIED
could not be fetched (developer.samsung.com other pages, docs.tizen.org and GitHub were
blocked by the sandbox/approval policy) and must be confirmed with the checklist at the end.

App: `client/tizen/spike/` (`config.xml`, `index.html`, `main.js`, `style.css`,
`protocol.js` copy, `icon.png` placeholder).

## Design choices

| Choice | Reason |
|---|---|
| Classic `<script>` files, no ES modules | Packaged apps load from `file://`-style origins, where module imports are the most likely thing to break and are not documented as supported for TV web apps; classic scripts are safe on any Tizen version. `protocol.js` is therefore a copy with `export` stripped and a `window.GhostpadProtocol` namespace |
| ES2017-level syntax (`var`, no `?.`/`??`, no optional catch binding) | Target Chromium on Tizen 9 is new, but older sideload targets (and the Tizen Studio emulator) are not; costs nothing |
| `required_version="6.0"` | Accepts any Tizen 6.0+ TV; the U8500F should be Tizen 9.0 (UNVERIFIED, check Settings > About TV). Namespaces, id/package format (10-char package id), `tizen:profile`, `<access origin>`, CSP and `screen.size.all` are all UNVERIFIED (not fetched) |
| Transparent page, video `zPosition: "BEHIND"` first | Verified: default `zPosition` is FRONT, `BEHIND` places the TV window behind the web app (since 2.4); BEHIND is the only way to draw an overlay over gameplay. Whether 2025 TVs honour it is UNVERIFIED |
| `<access origin="*">` | Spike only; narrow to the PC IP in M5 |

Regenerate `protocol.js` after protocol changes (from repo root; then re-add the header
comment, or just copy the header from the existing file):

```
sed -e 's/^export //' -e 's/} catch {/} catch (e) {/' client/web/protocol.js
```

## Remote keys in the spike

| Key | Action |
|---|---|
| Info or Green (`i`/`g` on a PC keyboard) | Cycle overlay: full, hidden, mini line, ... |
| Yellow (`y`) | Toggle video z-order BEHIND / FRONT (re-calls `show`) |
| Blue (`b`) | Hide / show the TV window |
| Red (`r`) | Re-run detection and source listing |
| Arrows + Enter | Pick HDMI source while the picker is visible (first HDMI auto-picked after 5 s) |
| Back | Hide overlay; press again to exit |

Info/colour keys are registered with `tizen.tvinputdevice.registerKey(name)` using names
`Info`, `ColorF0Red`, `ColorF1Green`, `ColorF2Yellow`, `ColorF3Blue`. Codes are resolved at
runtime via `tizen.tvinputdevice.getKey(name).code`; hard-coded numbers (Info 457, Red 403,
Green 404, Yellow 405, Blue 406, Back 10009) are only a fallback. UNVERIFIED (page not fetched):
key names, `getKey`/`registerKey`/`getSupportedKeys` signatures, Back = 10009. The app logs
`getSupportedKeys()` and any code that differs from the fallback; compare.

Gamepad-as-Back: whether controller B also arrives as a Back key event is UNVERIFIED (not
documented in anything read). The app defensively ignores a Back keydown while any gamepad
button is held or was pressed within 400 ms, so B should not exit the app. Test: press B, see
the log line "Back ignored".

## TVWindow / VIDEOSOURCE facts (verified, TVWindow API page)

| Item | Verified shape |
|---|---|
| `getPropertyValue("VIDEOSOURCE", cb)` | `cb(info)`; `info.connected` is an array of sources; Samsung's example loops it and matches `type === "HDMI"` and passes the entry straight to `setSource`. `disconnected` array, the `name` field and the full VideoSourceType enum are UNVERIFIED (systeminfo page not fetched); main.js logs the raw JSON |
| `setSource(videoSource, successCb(source, type), errorCb?, type?)` | `type` only `"MAIN"` |
| `show(successCb(rect, type), errorCb?, rectangle?, type?, zPosition?)` | `rectangle` = 4 strings `[x, y, w, h]`, each `px` or `%` (unitless = absolute), e.g. `["0","0px","50%","540px"]`; omitted = last rect or full screen; errors `InvalidValuesError`, `NotSupportedError`, `TypeMismatchError` |
| `hide(successCb, errorCb?, type?)`, `getRect(successCb, errorCb?, unit?, type?)` | as written |
| `getSource(type?)` | **synchronous**, returns `{type, number, ...}` |
| `getVideoResolution(type?)` | **synchronous**, returns `{width, height, frequency, ...}` (the code previously used a callback: fixed). `aspectRatio` field name is UNVERIFIED |
| `addVideoResolutionChangeListener(cb(res, type), type?)` / `remove...(id)` | The API is the resolution listener. **There is no `addVideoSourceChangeListener`** |
| Privilege | `http://tizen.org/privilege/tv.window` (public) |
| Support | "TV, HTV, Signage"; no model/firmware list. Check with `tizen.systeminfo.getCapability()`; PIP-dependent apps declare `http://tizen.org/feature/tv.pip` in config.xml (the spike does not, so it still installs; main.js logs the capability) |

Source: https://developer.samsung.com/tv/develop/api-references/tizen-web-device-api-references/tvwindow-api

## Prerequisites

- Samsung TV and PC on the same LAN. PC IP known. Windows firewall allows TCP 47800 if you
  test the WebSocket (daemon: `python -m ghostpad --backend mock --verbose` from `daemon/`).
- Tizen Studio with **TV Extensions** (Package Manager > Extension SDK) and **Certificate
  Manager**, or the standalone Tizen CLI. `tizen` and `sdb` live in `tizen-studio/tools/ide/bin`
  and `tizen-studio/tools`.
- A free Samsung account (needed for the Samsung certificate).
- The TV's **DUID** (shown when you create the Samsung certificate over `sdb`, see below).
- Controller paired to the TV (Settings > General > External Device Manager > Game
  Controller Settings, or Bluetooth).

## 1. Enable Developer Mode on the TV

1. Smart Hub > Apps.
2. With the remote, press **1 2 3 4 5** (use the on-screen number pad if needed). A
   Developer Mode dialog appears.
3. Switch **Developer mode: On**, enter the **Host PC IP** (the PC running Tizen Studio/sdb),
   OK, then **power-cycle the TV** (hold power, not standby).
4. Reopen Apps; a "Develop mode" banner shows. Port 26101 on the TV must be reachable from the PC.

(Procedure from memory; UNVERIFIED for 2025 firmware, doc page could not be fetched. Menu paths differ per Tizen version. Newer TVs may also require Samsung-account login or only accept certificates created via Tizen Studio Certificate Manager: UNVERIFIED.)

## 2. Certificates (never commit them)

A TV needs an author certificate plus a **Samsung distributor certificate that contains the TV's
DUID**. A plain Tizen certificate is rejected on TVs.

### Tizen Studio route

1. Connect the TV: Device Manager > Remote Device Manager > `+`, name, IP = TV IP, port 26101.
   Toggle the connection on.
2. Tools > Certificate Manager > `+` > **Samsung** certificate > TV > create a new author certificate
   (password) > distributor certificate: sign in with the Samsung account, and select the connected
   TV so its DUID is included. Finish; this creates a profile (e.g. `ghostpad`).
3. Right-click the project > Run As > Tizen Web Application (Studio signs with the active profile).

### CLI route

```
# connect and check
sdb connect <TV_IP>
sdb devices                       # should list <TV_IP>:26101 as the TV model
sdb capability                    # shows platform/version; note it

# author certificate (once)
tizen certificate -a ghostpad -p <password> -c <country> -ct <city> -n <name> -e <email> -f ghostpad-author

# The Samsung distributor certificate (with DUID) is created through Certificate Manager (GUI,
# Samsung account login). Afterwards create/list the profile:
tizen security-profiles list
# or add manually:
tizen security-profiles add -n ghostpad -a <author.p12> -p <author_pw> -d <distributor.p12> -dp <dist_pw>

# package (signs with the profile)
cd client/tizen/spike
tizen package -t wgt -s ghostpad -- .
# output: ./GhostpadSpike.wgt (git-ignored via *.wgt)

# install and run
tizen install -n GhostpadSpike.wgt -t <target-name-from-sdb-devices>
tizen run -p GhstPadSpk.Spike -t <target-name>
```

The CLI flags above are from memory and UNVERIFIED (not fetched this pass); run
`tizen help package` / `tizen help certificate` if a flag is rejected. The package output
folder/name may differ. If `tizen package` leaves `author-signature.xml`/`signature1.xml`
in the project dir they are git-ignored; do not commit them.

If install fails with a certificate error, the DUID in the distributor cert is wrong or missing:
regenerate it with the TV connected.

## 3. Web inspector

- Studio: right-click project > Run As > Tizen Web Application, or Debug As. Studio opens
  Chromium DevTools.
- CLI: `tizen run -p GhstPadSpk.Spike -t <target> -d` (debug) prints a port; or
  `sdb -s <target> forward tcp:9222 tcp:<port>` and open `http://localhost:9222` in Chrome (inspect).
- Alternatively TV: Apps > (selected app) > the "Debug" flow of the TV's developer menu. UNVERIFIED which of these works on 2025 firmware.
- All output also goes to the console with the prefix `[spike]`. In the console, `setPcHost('192.168.1.50')`
  enables the WebSocket test without rebuilding.

## 4. What to expect on screen

Overlay lists: tizen object / `tizen.tvwindow` / `tizen.systeminfo` / `tizen.tvinputdevice`, VIDEOSOURCE read,
HDMI found, `setSource`, `show BEHIND`, (`show FRONT` only on fallback), live gamepad data (id, mapping,
timestamp, buttons, axes), rAF/s and 50 ms timer ticks/s, `document.hidden` log, resolution from
`tvwindow.getVideoResolution` if present, and WebSocket state, counters and RTT (RTT minus daemon `proc_us`).

## Go/no-go checklist (fill in)

Date: ____  TV firmware: ____  Tizen version: ____  Controller: ____  PC GPU/output: ____

| # | Check | How | Result | Notes |
|---|---|---|---|---|
| 1 | `tizen.tvwindow` present, `tv.window` privilege accepted | Overlay step line is OK; no SecurityError | ☐ pass ☐ fail | |
| 2 | HDMI picture shown inside the app | `setSource` OK + `show` OK and PC desktop visible, full screen, correct aspect. Record which z-order works: ☐ BEHIND with overlay on top ☐ FRONT only | ☐ pass ☐ fail | |
| 3 | Gamepad input updates while HDMI shown | `gamepad updates/s` and button list change when pressing buttons with the video visible; `mapping="standard"` | ☐ pass ☐ fail | |
| 4 | HDMI audio plays | Hear PC audio (play a video on the PC) with the TV app in front; no mute/silence | ☐ pass ☐ fail | |
| 5 | Picture mode / Game Mode / ALLM and lag | See below | ☐ pass ☐ fail | |
| 6 | VRR / resolution / HDR passthrough | See below | ☐ pass ☐ fail | |
| 7 | App stays alive 30+ min | See below | ☐ pass ☐ fail | |
| 8 | WebSocket to PC works | `PC_HOST` set; overlay shows `welcome`, RTT updating; mock daemon log shows STATE; a Windows game controller tester sees the pad if using vigem | ☐ pass ☐ fail | |

### 5. Game Mode / lag

1. With the app showing HDMI, open the TV quick settings (Settings > Connection > Game Mode
   Settings, or the Game Bar) and note whether it reports Game Mode on/off, ALLM, VRR and input
   signal info. Compare to the same PC on the normal HDMI source (Source button), same picture mode.
2. Record the picture mode shown in each case ("Game" vs "Standard"/other) and whether
   motion-smoothing/AI upscaling appear to be applied (obvious soap-opera look, changed colours).
3. Lag test, **240 fps phone method**: film the screen and a physical button/key press together
   with an iPhone slow-motion (240 fps). Count frames from visible press to on-screen response, using
   a PC test app (e.g. a window that flashes on keypress, or a Windows controller tester).
   Do this for (a) normal HDMI source and (b) HDMI inside the app. At least 10 presses each; report
   median/worst in ms. Per CLAUDE.md, record measured numbers only.
4. Pass: in-app lag not noticeably worse than normal HDMI and Game Mode preserved. Fail: Game Mode lost
   and lag clearly higher.

### 6. VRR / resolution / HDR

Record for the in-app path vs normal HDMI: max resolution (e.g. 3840x2160 @ 60/120) and what
`getVideoResolution` reports; whether VRR indicator appears; HDR on/off and whether it shows the PC's HDR
signalling correctly; chroma/colour banding changes. None of these are required for v1 (1080p60 SDR is
enough); note them as observations.

### 7. Soak

Leave the app showing HDMI with the overlay on mini mode for 30+ minutes, with the PC idle and then with
gamepad activity. Check: `rAF/s` still ~60, timers ~20/s, `document.hidden` log empty, uptime keeps counting,
the TV does not start a screensaver/ambient mode/auto-dim/auto power-off, the app is not killed, and gamepad
updates/s still moves on input. Note TV settings: Eco Solution > Auto Power Off, Screensaver, Standby timer.

## Fallbacks if no-go

| Failure | Option |
|---|---|
| `tizen.tvwindow` missing, or `setSource`/`show` not working (checks 1, 2) | Cheap unlocked **Android TV/Google TV stick** on the TV's HDMI input running a client that reads the controller (paired to the stick) and talks to the daemon; the PC keeps its own HDMI path |
| Game Mode lost or lag clearly worse in-app (check 5) | Keep the PC on the normal HDMI source, and move the controller path off the TV: a **Raspberry Pi** (or mini PC) with the controller attached by USB/Bluetooth, forwarding to the PC with **usbip** or a Linux ghostpad client |
| Input stops while HDMI shown, or app is killed (checks 3, 7) | Same as above (controller on a separate always-foreground device) |
| Bluetooth range/latency | A **long-range 2.4 GHz dongle** (vendor USB receiver) plugged into the PC, no network hop; simplest and lowest latency if the PC is within range |

Pick based on which check failed; record it here with a replanned M5.

## Sources used

Fetched and read in this session:

- TVWindow API (Samsung): https://developer.samsung.com/tv/develop/api-references/tizen-web-device-api-references/tvwindow-api
  (signatures `setSource(videoSource, successCb, errorCb?, type?)`, `show(successCb, errorCb?, rectangle?, type?, zPosition?)`,
  `hide`, `getRect`, `getVideoResolution`; `WindowType` only `"MAIN"`; `ZPosition` `"FRONT"|"BEHIND"` since 2.4; privilege `tv.window`;
  listed support: TV, HTV Signage; says nothing about 2024/2025 models)
- Managing PiP Windows (Samsung): https://developer.samsung.com/SmartTV/develop/guides/pip-picture-in-picture/managing-pip-windows.html
  (VIDEOSOURCE `connected` list, pick HDMI, `setSource`; "the PiP window is created on top of and covers HTML elements"; `tv.audio`
  privilege only for volume control)
- Search result titles also seen, not read: https://developer.samsung.com/tv/develop/getting-started/quick-start-guide

**Not retrieved** (WebFetch needed approval nobody answered; curl to developer.samsung.com, docs.tizen.org and
raw.githubusercontent.com was refused by the egress proxy; WebSearch returns titles only). The following are from memory and
UNVERIFIED: `SystemInfoVideoSourceInfo` fields and VideoSourceType enum, `tizen.tvinputdevice` names/codes/signatures
(candidate: https://developer.samsung.com/tv/develop/api-references/tizen-web-device-api-references/tvinputdevice-api/),
Gamepad API behaviour on Samsung TVs (`getGamepads`, `mapping`), config.xml/Tizen 9 specifics (CSP, `tizen:allow-navigation`),
classic scripts vs ES modules and the Chromium version per model year, sideloading steps and certificate lifetime, community
installers (e.g. Apps2Samsung, Samsung-Jellyfin-Installer; jellyfin-tizen / moonlight-tizen READMEs), tvwindow support on
2024/2025 models, Game Mode/ALLM/audio with in-app HDMI, multitasking/suspend. Start from:

- https://developer.samsung.com/smarttv/develop/api-references/tizen-web-device-api-references/systeminfo-api.html
- https://developer.samsung.com/smarttv/develop/guides/user-interaction/remote-control.html
- https://developer.samsung.com/smarttv/develop/getting-started/setting-up-sdk/creating-certificates.html
- https://developer.samsung.com/smarttv/develop/getting-started/using-sdk/tv-device.html
