"""CLI entry point: python -m ghostpad [options]."""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .backends.base import BackendUnavailable, PadBackend
from .server import DEFAULT_WEB_ROOT, GhostpadServer
from .stats import Stats

log = logging.getLogger("ghostpad")


def make_backend(choice: str) -> PadBackend:
    from .backends.mock import MockBackend

    if choice == "mock":
        return MockBackend()
    if choice == "vigem":
        from .backends.vigem import VigemBackend
        return VigemBackend()  # BackendUnavailable propagates: explicit choice, no fallback
    # auto
    if sys.platform == "win32":
        try:
            from .backends.vigem import VigemBackend
            return VigemBackend()
        except BackendUnavailable as e:
            log.warning("%s; falling back to the mock backend (no real virtual pad)", e)
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
    p.add_argument("--tray", action="store_true",
                   help="Windows: run with a notification-area icon (status, log, autostart, quit)")
    p.add_argument("--log-file", default=None,
                   help="also log to this file (rotated at 1 MB, 3 kept); "
                        r"with --tray the default is %%LOCALAPPDATA%%\ghostpad\ghostpad.log")
    return p.parse_args(argv)


def build_server(args: argparse.Namespace) -> GhostpadServer:
    return GhostpadServer(
        make_backend(args.backend), host=args.bind, port=args.port, token=args.token,
        web_root=args.web_root, stats=Stats(), stats_interval=30.0 if args.verbose else None,
        trace=args.trace,
    )


def setup_logging(args: argparse.Namespace) -> Path | None:
    """Console logging, plus a rotating file with --log-file or --tray. Returns the file."""
    path = Path(args.log_file) if args.log_file else None
    if path is None and args.tray:
        from .tray import default_log_path
        path = default_log_path()
    handlers: list[logging.Handler] = []
    if path is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(RotatingFileHandler(path, maxBytes=1_000_000, backupCount=3, encoding="utf-8"))
    # A windowed (tray) process has no console: sys.stderr is None or a redirect file.
    if not handlers or (sys.stderr is not None and (not args.tray or sys.stderr.isatty())):
        handlers.append(logging.StreamHandler())
    logging.basicConfig(
        level=logging.DEBUG if (args.verbose or args.trace) else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
    )
    return path


async def run(args: argparse.Namespace) -> None:
    server = build_server(args)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):
            pass  # Windows: KeyboardInterrupt cancels this task; finally still runs
    await server.start()
    old_break = None
    if sys.platform == "win32":
        # Ctrl+Break otherwise ends the process at once (0xC000013A), skipping the
        # neutral-then-unplug release below. Signal handlers wake the proactor loop.
        old_break = signal.signal(signal.SIGBREAK, lambda *_: loop.call_soon_threadsafe(stop.set))
    try:
        await stop.wait()
    finally:
        await server.stop()
        if old_break is not None:
            signal.signal(signal.SIGBREAK, old_break)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    log_path = setup_logging(args)
    if args.tray:
        from .tray import default_log_path, run_tray
        return run_tray(args, log_path or default_log_path(), lambda: build_server(args))
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        pass
    except BackendUnavailable as e:
        log.error("backend unavailable: %s", e)
        return 2
    except OSError as e:
        log.error("cannot start server: %s", e)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
