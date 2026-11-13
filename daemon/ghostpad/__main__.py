"""CLI entry point: python -m ghostpad [options]."""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys

from .backends.base import PadBackend
from .server import DEFAULT_WEB_ROOT, GhostpadServer
from .stats import Stats

log = logging.getLogger("ghostpad")


def make_backend(choice: str) -> PadBackend:
    from .backends.mock import MockBackend

    if choice == "mock":
        return MockBackend()
    if choice == "vigem":
        from .backends.vigem import VigemBackend
        return VigemBackend()
    # auto
    if sys.platform == "win32":
        try:
            from .backends.vigem import VigemBackend
            return VigemBackend()
        except ImportError:
            log.warning("vgamepad not importable; falling back to the mock backend")
    else:
        log.warning("not on Windows; using the mock backend (no real virtual pad)")
    return MockBackend()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="ghostpad", description="Controller-over-LAN daemon")
    p.add_argument("--port", type=int, default=47800)
    p.add_argument("--bind", default="0.0.0.0")
    p.add_argument("--backend", choices=["auto", "vigem", "mock"], default="auto")
    p.add_argument("--token", default=None, help="require ?token=SECRET on /ws")
    p.add_argument("--web-root", default=str(DEFAULT_WEB_ROOT))
    p.add_argument("--verbose", action="store_true")
    p.add_argument("--trace", action="store_true",
                   help="log every pad connect/update/neutral/unplug at DEBUG (noisy)")
    return p.parse_args(argv)


async def run(args: argparse.Namespace) -> None:
    backend = make_backend(args.backend)
    server = GhostpadServer(
        backend, host=args.bind, port=args.port, token=args.token, web_root=args.web_root,
        stats=Stats(), stats_interval=30.0 if args.verbose else None, trace=args.trace,
    )
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):
            pass  # Windows: KeyboardInterrupt cancels this task; finally still runs
    await server.start()
    try:
        await stop.wait()
    finally:
        await server.stop()


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if (args.verbose or args.trace) else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        pass
    except ImportError as e:
        log.error("backend unavailable: %s", e)
        return 2
    except OSError as e:
        log.error("cannot start server: %s", e)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
