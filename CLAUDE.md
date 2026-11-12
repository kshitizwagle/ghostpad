# CLAUDE.md

Guidance for AI coding agents working in this repo.

## Project

ghostpad forwards game controller input over the LAN to a virtual Xbox 360 pad on a
Windows PC. The PC is wired to a Samsung Tizen TV over HDMI (video and audio), and
the controller is paired to the TV. A client on the TV reads the controller with the
Gamepad API and sends its state over a WebSocket to a Python daemon on the PC, which
drives the virtual pad via ViGEm. Input only; no streaming.

## Read first

| File | Purpose |
|---|---|
| `PRODUCT.md` | Scope, goals, non-goals, success criteria |
| `DESIGN.md` | Architecture, protocol v1, latency budget. **Source of truth for the protocol.** Change the doc and the code together |
| `PLAN.md` | Milestones and task checklists |

## Layout

```
daemon/ghostpad/   Python package (server, protocol, backends, watchdog, CLI)
daemon/tests/      unittest suite, must run without Windows
client/web/        browser test client, static, no build step
client/tizen/      Tizen TV app (later)
bench/             latency scripts
docs/              procedures and findings
```

## Commands

```
cd daemon && python -m unittest discover -s tests -v   # run tests
python -m ghostpad --backend mock --verbose            # run locally (from daemon/)
```

The `mock` backend needs no driver and works on any OS. The `vigem` backend needs
Windows and ViGEmBus.

## Conventions

- **Daemon networking is stdlib only.** The WebSocket server is hand-written
  (RFC 6455). Do not add `websockets`, `aiohttp` or similar.
- **No new runtime dependencies without discussion.** The only one is `vgamepad`,
  imported lazily so non-Windows runs and tests never need it.
- Python 3.11+, `asyncio`, type hints on all signatures.
- Binary protocol is little-endian; use `struct` with explicit `<` formats.
- `client/web/` has **no build step**: plain ES modules, vanilla JS, no bundler,
  no framework, no npm dependencies.
- Tests must not need Windows, real sockets to the outside, or a gamepad. Use the
  `mock` backend.
- Keep docs concise and technical: tables and lists, no filler.

## Rules

- **Never commit Tizen signing material** (`*.p12`, `author-signature.xml`,
  `signature1.xml`, `.sign/`). `.gitignore` covers these; do not work around it.
- **Bugs:** log them as GitHub issues. Minor ones that don't affect main
  functionality or usability are deferred; blockers are fixed immediately.
- **PLAN.md:** tick the checkboxes when you complete tasks, in the same PR.
- **Protocol changes:** update `DESIGN.md` and the code together, and bump the
  version if the wire format changes incompatibly.
- **Branches and PRs:** each milestone lands via its own branch and PR.
- **PR links:** before giving a PR link, check whether that PR is already merged. If
  it is, open a new PR for any further changes instead of pushing to the merged one.
- **Measure before claiming latency numbers.** Use the M3 tooling (HUD, daemon
  histogram, `bench/`, button-to-photon). Do not state latency figures from reasoning
  alone.
- **Fail safe is a hard requirement:** a dropped link must never leave a button held.
  Do not weaken the watchdog (200 ms) or the close-time neutral-then-unplug behaviour.
- Do not commit logs or benchmark output (`logs/`, `bench/results/`).
