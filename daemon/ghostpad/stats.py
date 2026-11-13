"""recv->apply duration recorder. Kept small; M3 extends it."""

from __future__ import annotations

from collections import deque


class Stats:
    """Records durations in ns (bounded to the most recent `capacity` samples)."""

    def __init__(self, capacity: int = 200_000) -> None:
        self._samples: deque[int] = deque(maxlen=capacity)
        self.count = 0

    def record(self, ns: int) -> None:
        self._samples.append(ns)
        self.count += 1

    def summary(self) -> str:
        if not self._samples:
            return "recv->apply: no samples"
        s = sorted(self._samples)
        n = len(s)

        def pct(p: float) -> int:
            return s[min(n - 1, int(p * n))]

        def us(ns: int) -> str:
            return f"{ns / 1000:.1f}us"

        return (
            f"recv->apply: count={self.count} min={us(s[0])} median={us(s[n // 2])} "
            f"p95={us(pct(0.95))} p99={us(pct(0.99))} max={us(s[-1])}"
        )
