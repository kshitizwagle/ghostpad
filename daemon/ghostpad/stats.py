"""Daemon counters and the recv->apply duration recorder (DESIGN.md "Measurement design").

Hot path cost: `record()` is one deque append and one int increment; the counters are
plain int attributes bumped by the server. Sorting happens only in `snapshot()`/`summary()`.
"""

from __future__ import annotations

import json
from collections import deque
from time import monotonic


def _pct(sorted_samples: list[int], p: float) -> int:
    return sorted_samples[min(len(sorted_samples) - 1, int(p * len(sorted_samples)))]


class Stats:
    """Records durations in ns (bounded to the most recent `capacity` samples) plus counters."""

    def __init__(self, capacity: int = 200_000) -> None:
        self._samples: deque[int] = deque(maxlen=capacity)
        self.count = 0  # recv->apply samples recorded (all time)
        self.frames = 0  # binary frames received from active clients
        self.stale_drops = 0  # STATE dropped by the seq filter
        self.malformed_drops = 0  # undecodable / unknown / bad-pad frames
        self.prehello_drops = 0  # binary frames before a valid hello
        self.watchdog_trips = 0
        self.pings = 0
        self._t0 = monotonic()

    def record(self, ns: int) -> None:
        self._samples.append(ns)
        self.count += 1

    @property
    def uptime_s(self) -> float:
        return monotonic() - self._t0

    def copy_samples(self) -> list[int]:
        """Cheap copy (call on the event-loop thread); sort the copy elsewhere."""
        return list(self._samples)

    def snapshot(self, samples: list[int] | None = None) -> dict:
        """JSON-serialisable snapshot. Durations in microseconds."""
        s = sorted(self._samples if samples is None else samples)
        if s:
            def us(ns: int) -> float:
                return round(ns / 1000, 1)
            recv = {
                "count": self.count, "window": len(s), "min_us": us(s[0]),
                "median_us": us(s[len(s) // 2]), "p95_us": us(_pct(s, 0.95)),
                "p99_us": us(_pct(s, 0.99)), "max_us": us(s[-1]),
            }
        else:
            recv = {"count": self.count, "window": 0, "min_us": None, "median_us": None,
                    "p95_us": None, "p99_us": None, "max_us": None}
        return {
            "recv_apply": recv,
            "frames_received": self.frames,
            "stale_drops": self.stale_drops,
            "malformed_drops": self.malformed_drops,
            "prehello_drops": self.prehello_drops,
            "watchdog_trips": self.watchdog_trips,
            "ping_count": self.pings,
            "uptime_s": round(self.uptime_s, 1),
        }

    def snapshot_json(self, samples: list[int] | None = None) -> str:
        return json.dumps(self.snapshot(samples), separators=(",", ":"))

    def summary(self) -> str:
        r = self.snapshot()
        ra = r["recv_apply"]
        if not ra["window"]:
            head = "recv->apply: no samples"
        else:
            head = (f"recv->apply: count={ra['count']} min={ra['min_us']}us median={ra['median_us']}us "
                    f"p95={ra['p95_us']}us p99={ra['p99_us']}us max={ra['max_us']}us")
        return (f"{head} frames={r['frames_received']} stale={r['stale_drops']} "
                f"malformed={r['malformed_drops']} watchdog={r['watchdog_trips']} "
                f"pings={r['ping_count']} uptime={r['uptime_s']}s")
