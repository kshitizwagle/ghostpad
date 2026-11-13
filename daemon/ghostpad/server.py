"""Single-port server: static HTTP GET + WebSocket at /ws. See DESIGN.md."""

from __future__ import annotations

import asyncio
import hmac
import logging
import mimetypes
from pathlib import Path
from time import perf_counter_ns
from urllib.parse import parse_qs, unquote, urlsplit

from . import protocol as proto
from . import wsproto
from .backends.base import PadBackend
from .stats import Stats

log = logging.getLogger(__name__)

CLOSE_VERSION = 4000
CLOSE_TAKEN_OVER = 4001
CLOSE_BAD_TOKEN = 4002
CLOSE_HELLO_TIMEOUT = 4003

WATCHDOG_TIMEOUT_NS = 200_000_000
WATCHDOG_TICK_S = 0.010
HELLO_TIMEOUT_S = 5.0
MAX_HEAD = 16 * 1024
CLIENT_LOG_BURST = 50  # client "log" lines accepted at once
CLIENT_LOG_RATE = 20.0  # then per second

client_log = logging.getLogger("ghostpad.client")

DEFAULT_WEB_ROOT = Path(__file__).resolve().parents[2] / "client" / "web"

_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".htm": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".txt": "text/plain; charset=utf-8",
    ".webmanifest": "application/manifest+json",
    ".wasm": "application/wasm",
}

_REASONS = {200: "OK", 400: "Bad Request", 403: "Forbidden", 404: "Not Found", 405: "Method Not Allowed",
            426: "Upgrade Required", 431: "Request Header Fields Too Large"}


class Client:
    __slots__ = ("ws", "name", "active", "plugged", "last_seq", "neutralized",
                 "last_input_ns", "rumble", "log_tokens", "log_t", "log_dropped")

    def __init__(self, ws: wsproto.WebSocket, max_pads: int) -> None:
        self.ws = ws
        self.name = "?"
        self.active = False  # True between a valid hello and release
        self.plugged = [False] * max_pads
        self.last_seq: list[int | None] = [None] * max_pads
        self.neutralized = False
        self.last_input_ns = perf_counter_ns()  # last STATE / PAD_GONE (not PING)
        self.rumble: list[tuple[int, int]] = [(0, 0)] * max_pads  # last RUMBLE sent per pad
        self.log_tokens = float(CLIENT_LOG_BURST)  # client "log" messages: token bucket
        self.log_t = perf_counter_ns()
        self.log_dropped = 0


class GhostpadServer:
    def __init__(
        self,
        backend: PadBackend,
        host: str = "0.0.0.0",
        port: int = 47800,
        token: str | None = None,
        web_root: Path | str | None = None,
        stats: Stats | None = None,
        watchdog_ms: float = 200,
        stats_interval: float | None = None,
        hello_timeout: float = HELLO_TIMEOUT_S,
        trace: bool = False,
    ) -> None:
        self.backend = backend
        self.host = host
        self.port = port
        self.token = token
        self.web_root = Path(web_root) if web_root else DEFAULT_WEB_ROOT
        self.stats = stats if stats is not None else Stats()
        self._watchdog_ns = int(watchdog_ms * 1_000_000)
        self._stats_interval = stats_interval
        self._hello_timeout = hello_timeout
        self._trace = trace
        self._welcome = proto.encode_welcome(backend.name, backend.max_pads)
        self._owner: Client | None = None
        self._clients: set[Client] = set()
        self._server: asyncio.Server | None = None
        self._tasks: list[asyncio.Task] = []
        self._loop: asyncio.AbstractEventLoop | None = None
        self.dropped = 0

    # -- lifecycle ----------------------------------------------------------

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._handle, self.host, self.port, limit=wsproto.MAX_FRAME + 1024
        )
        self.port = self._server.sockets[0].getsockname()[1]
        self._loop = asyncio.get_running_loop()
        self.backend.set_rumble_handler(self._rumble_from_backend)
        self._tasks.append(asyncio.create_task(self._watchdog_loop()))
        if self._stats_interval:
            self._tasks.append(asyncio.create_task(self._stats_loop()))
        log.info("listening on %s:%d (backend=%s, web root=%s)",
                 self.host, self.port, self.backend.name, self.web_root)

    async def stop(self) -> None:
        """Neutralise and unplug everything, close connections and backend."""
        self.backend.set_rumble_handler(None)
        if self._server is not None:
            self._server.close()
        for t in self._tasks:
            t.cancel()
        for c in list(self._clients):
            self._release(c)
            c.ws.send_close(wsproto.CLOSE_NORMAL, "server shutdown")
        self._owner = None
        try:
            self.backend.close()
        except Exception:
            log.exception("backend close failed")
        for c in list(self._clients):
            c.ws.abort()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        if self._server is not None:
            try:
                await asyncio.wait_for(self._server.wait_closed(), 2.0)
            except asyncio.TimeoutError:
                pass
        log.info("exit stats: %s", self.stats.snapshot_json())

    # -- background tasks ---------------------------------------------------

    async def _watchdog_loop(self) -> None:
        while True:
            await asyncio.sleep(WATCHDOG_TICK_S)
            c = self._owner
            if c is None or c.neutralized or not any(c.plugged):
                continue
            if perf_counter_ns() - c.last_input_ns >= self._watchdog_ns:
                c.neutralized = True
                self.stats.watchdog_trips += 1
                log.warning("watchdog: no input from %r, pads neutral", c.name)
                self._neutral_all(c)

    async def _stats_loop(self) -> None:
        assert self._stats_interval
        while True:
            await asyncio.sleep(self._stats_interval)
            log.info(self.stats.summary())

    # -- pad helpers (sync; backend exceptions never escape) ------------------

    def _neutral_all(self, c: Client) -> None:
        for pad, on in enumerate(c.plugged):
            if on:
                if self._trace:
                    log.debug("trace pad %d neutral (watchdog)", pad)
                try:
                    self.backend.neutral(pad)
                except Exception:
                    log.exception("backend.neutral(%d) failed", pad)
                self._rumble_off(c, pad)

    def _release(self, c: Client) -> None:
        """Neutral then unplug every pad of `c`. Idempotent."""
        c.active = False
        for pad, on in enumerate(c.plugged):
            if on:
                c.plugged[pad] = False
                c.last_seq[pad] = None
                if self._trace:
                    log.debug("trace pad %d neutral, unplug (release)", pad)
                try:
                    self.backend.neutral(pad)
                    self.backend.disconnect(pad)
                except Exception:
                    log.exception("backend release of pad %d failed", pad)
                self._rumble_off(c, pad)
        if self._owner is c:
            self._owner = None

    # -- rumble (backend -> owning client) --------------------------------------

    def _rumble_from_backend(self, pad: int, large: int, small: int) -> None:
        """RumbleHandler: may run on a driver thread, so hop onto the loop."""
        loop = self._loop
        if loop is None:
            return
        try:
            loop.call_soon_threadsafe(self._on_rumble, pad, large, small)
        except RuntimeError:
            pass  # loop closed during shutdown

    def _on_rumble(self, pad: int, large: int, small: int) -> None:
        c = self._owner
        # While the watchdog holds the pads neutral the client counts as stalled: no rumble
        # until its next STATE (changes from the game after that are sent as usual).
        if c is None or c.neutralized or not 0 <= pad < len(c.plugged) or not c.plugged[pad]:
            return
        self._send_rumble(c, pad, large & 0xFF, small & 0xFF)

    def _rumble_off(self, c: Client, pad: int) -> None:
        if c.rumble[pad] != (0, 0):
            self._send_rumble(c, pad, 0, 0)

    def _send_rumble(self, c: Client, pad: int, large: int, small: int) -> None:
        """Send on change only; the client re-issues the effect while it is non-zero."""
        if c.rumble[pad] == (large, small):
            return
        c.rumble[pad] = (large, small)
        self.stats.rumble_sent += 1
        if self._trace:
            log.debug("trace pad %d rumble large=%d small=%d", pad, large, small)
        c.ws.send_binary_nowait(proto.encode_rumble(pad, large, small))

    # -- connection entry ---------------------------------------------------

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            try:
                head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 10.0)
            except asyncio.LimitOverrunError:
                await self._respond(writer, 431, b"headers too large")
                return
            except (asyncio.IncompleteReadError, asyncio.TimeoutError, ConnectionError):
                return
            if len(head) > MAX_HEAD:
                await self._respond(writer, 431, b"headers too large")
                return
            try:
                lines = head.decode("latin-1").split("\r\n")
                method, target, _version = lines[0].split(" ", 2)
                headers: dict[str, str] = {}
                for line in lines[1:]:
                    if not line:
                        continue
                    k, _, v = line.partition(":")
                    headers[k.strip().lower()] = v.strip()
            except ValueError:
                await self._respond(writer, 400, b"bad request")
                return
            url = urlsplit(target)
            if url.path == "/stats":
                await self._handle_stats(writer, method, url.query)
            elif url.path == "/ws":
                await self._handle_ws(reader, writer, method, url.query, headers)
            else:
                await self._handle_http(writer, method, url.path)
        except (ConnectionError, OSError):
            pass
        except Exception:
            log.exception("connection handler failed")
        finally:
            writer.close()

    # -- HTTP -----------------------------------------------------------------

    async def _respond(self, writer: asyncio.StreamWriter, status: int, body: bytes,
                       ctype: str = "text/plain; charset=utf-8", head_only: bool = False,
                       extra: str = "") -> None:
        head = (
            f"HTTP/1.1 {status} {_REASONS.get(status, 'Error')}\r\n"
            f"Content-Type: {ctype}\r\nContent-Length: {len(body)}\r\n"
            "Cache-Control: no-cache\r\nX-Content-Type-Options: nosniff\r\n"
            f"Connection: close\r\n{extra}\r\n"
        ).encode("latin-1")
        writer.write(head if head_only else head + body)
        try:
            await writer.drain()
        except (ConnectionError, OSError):
            pass

    def resolve_static(self, url_path: str) -> Path | None:
        """Map a URL path to a file under the web root; None if unsafe/missing."""
        path = unquote(url_path)
        if "\x00" in path or "\\" in path or ":" in path:
            return None
        parts = [p for p in path.split("/") if p]
        if ".." in parts or any(p == "." for p in parts):
            return None
        root = self.web_root.resolve()
        try:
            target = root.joinpath(*parts).resolve()
        except (OSError, ValueError):
            return None
        if target != root and not target.is_relative_to(root):
            return None
        if target.is_dir():
            target = target / "index.html"
        return target if target.is_file() else None

    def _token_ok(self, query: str) -> bool:
        if self.token is None:
            return True
        given = parse_qs(query).get("token", [""])[0]
        return hmac.compare_digest(given.encode(), self.token.encode())

    async def _handle_stats(self, writer: asyncio.StreamWriter, method: str, query: str = "") -> None:
        if not self._token_ok(query):
            await self._respond(writer, 403, b"forbidden")
            return
        if method not in ("GET", "HEAD"):
            await self._respond(writer, 405, b"method not allowed", extra="Allow: GET, HEAD\r\n")
            return
        # Sorting up to `capacity` samples is off the hot path but not free: copy on the loop
        # thread (cheap), sort in a worker so the watchdog is never blocked.
        samples = self.stats.copy_samples()
        loop = asyncio.get_running_loop()
        body = (await loop.run_in_executor(None, self.stats.snapshot_json, samples)).encode()
        await self._respond(writer, 200, body, "application/json", head_only=method == "HEAD")

    async def _handle_http(self, writer: asyncio.StreamWriter, method: str, path: str) -> None:
        if method not in ("GET", "HEAD"):
            await self._respond(writer, 405, b"method not allowed", extra="Allow: GET, HEAD\r\n")
            return
        target = self.resolve_static(path)
        if target is None:
            await self._respond(writer, 404, b"not found", head_only=method == "HEAD")
            return
        try:
            body = await asyncio.get_running_loop().run_in_executor(None, target.read_bytes)
        except OSError:
            await self._respond(writer, 404, b"not found", head_only=method == "HEAD")
            return
        ext = target.suffix.lower()
        ctype = _CONTENT_TYPES.get(ext) or mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        await self._respond(writer, 200, body, ctype, head_only=method == "HEAD")

    # -- WebSocket ------------------------------------------------------------

    async def _handle_ws(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                         method: str, query: str, headers: dict[str, str]) -> None:
        key = headers.get("sec-websocket-key")
        if (method != "GET" or headers.get("upgrade", "").lower() != "websocket"
                or "upgrade" not in headers.get("connection", "").lower() or not key):
            await self._respond(writer, 426, b"websocket upgrade required",
                                extra="Upgrade: websocket\r\n")
            return
        if headers.get("sec-websocket-version") != "13":
            await self._respond(writer, 426, b"unsupported websocket version",
                                extra="Sec-WebSocket-Version: 13\r\n")
            return
        writer.write(wsproto.handshake_response(key))
        ws = wsproto.WebSocket(reader, writer)
        client = Client(ws, self.backend.max_pads)
        self._clients.add(client)
        try:
            if not self._token_ok(query):
                log.warning("rejected client: bad token")
                await ws.close(CLOSE_BAD_TOKEN, "bad token")
                return
            await self._session(client)
        finally:
            self._release(client)
            self._clients.discard(client)

    async def _session(self, client: Client) -> None:
        ws = client.ws
        backend = self.backend
        max_pads = backend.max_pads
        stats = self.stats
        hello_deadline = asyncio.get_running_loop().time() + self._hello_timeout
        while True:
            if client.active:
                msg = await ws.recv()
            else:
                left = hello_deadline - asyncio.get_running_loop().time()
                try:
                    msg = await asyncio.wait_for(ws.recv(), max(left, 0.0))
                except asyncio.TimeoutError:
                    log.warning("no valid hello within %.1fs, closing", self._hello_timeout)
                    await ws.close(CLOSE_HELLO_TIMEOUT, "hello timeout")
                    return
            if msg is None:
                return
            op, data = msg
            if op == wsproto.OP_BIN:
                if not client.active:
                    self.dropped += 1
                    stats.prehello_drops += 1
                    continue
                stats.frames += 1
                t0 = ws.last_rx_ns
                m = proto.decode_binary(data)  # type: ignore[arg-type]
                cls = type(m)
                if cls is proto.StateFrame:
                    pad = m.pad
                    if pad >= max_pads:
                        self.dropped += 1
                        stats.malformed_drops += 1
                        continue
                    last = client.last_seq[pad]
                    if last is not None and not proto.seq_newer(m.seq, last):
                        self.dropped += 1
                        stats.stale_drops += 1
                        continue
                    client.last_seq[pad] = m.seq
                    client.last_input_ns = t0
                    try:
                        if not client.plugged[pad]:
                            if self._trace:
                                log.debug("trace pad %d connect", pad)
                            backend.connect(pad)
                            client.plugged[pad] = True
                        client.neutralized = False
                        if self._trace:
                            log.debug("trace pad %d update seq=%d %s", pad, m.seq, m.state)
                        backend.update(pad, m.state)
                    except Exception:
                        log.exception("backend update failed (pad %d)", pad)
                        continue
                    stats.record(perf_counter_ns() - t0)
                elif cls is proto.Ping:
                    stats.pings += 1
                    proc_us = (perf_counter_ns() - t0) // 1000
                    await ws.send_binary(proto.encode_pong(m.id, m.t_client, proc_us))
                elif cls is proto.PadGone:
                    client.last_input_ns = t0
                    self._pad_gone(client, m.pad)
                else:
                    self.dropped += 1
                    stats.malformed_drops += 1
            elif not client.active:
                hello = proto.parse_hello(data)  # type: ignore[arg-type]
                if hello is None:
                    self.dropped += 1
                    continue
                if not proto.hello_version_ok(hello):
                    log.warning("client %r: version mismatch (%r)", hello.get("client"), hello.get("version"))
                    await ws.close(CLOSE_VERSION, "unsupported protocol version")
                    return
                self._take_ownership(client, str(hello.get("client", "?"))[:64])
                await ws.send_text(self._welcome)
            else:
                self._client_text(client, data)  # type: ignore[arg-type]

    def _client_text(self, client: Client, text: str) -> None:
        """Text after hello: {"type":"log"} is logged (rate-limited); anything else is ignored."""
        msg = proto.parse_client_log(text)
        if msg is None:
            return
        now = perf_counter_ns()
        client.log_tokens = min(float(CLIENT_LOG_BURST),
                                client.log_tokens + (now - client.log_t) / 1e9 * CLIENT_LOG_RATE)
        client.log_t = now
        if client.log_tokens < 1:
            client.log_dropped += 1
            return
        client.log_tokens -= 1
        if client.log_dropped:
            client_log.warning("%s: %d log lines dropped (rate limit)", client.name, client.log_dropped)
            client.log_dropped = 0
        client_log.info("%s: %s", client.name, msg)

    def _take_ownership(self, client: Client, name: str) -> None:
        old = self._owner
        if old is not None and old is not client:
            log.info("client %r takes over from %r", name, old.name)
            self._release(old)
            old.ws.send_close(CLOSE_TAKEN_OVER, "taken over by a newer client")
        client.name = name
        client.active = True
        self._owner = client
        log.info("client %r connected", name)

    def _pad_gone(self, client: Client, pad: int) -> None:
        if self._trace:
            log.debug("trace pad %d PAD_GONE", pad)
        client.last_seq[pad] = None
        if client.plugged[pad]:
            client.plugged[pad] = False
            try:
                self.backend.neutral(pad)
                self.backend.disconnect(pad)
            except Exception:
                log.exception("pad_gone release failed (pad %d)", pad)
            self._rumble_off(client, pad)
