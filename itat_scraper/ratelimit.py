"""Sliding-window rate limiter — cap completed operations per hour."""
from __future__ import annotations

import time
from collections import deque

_WINDOW = 3600.0  # 1 hour in seconds


class RateLimiter:
    """At most `max_per_hour` operations within any 1-hour window."""

    def __init__(self, max_per_hour: int | None):
        self.max_per_hour = max_per_hour
        self.events: deque[float] = deque()

    def wait(self) -> float:
        """Block until another operation is allowed. Returns seconds slept."""
        if not self.max_per_hour or self.max_per_hour <= 0:
            return 0.0
        now = time.time()
        cutoff = now - _WINDOW
        while self.events and self.events[0] < cutoff:
            self.events.popleft()
        if len(self.events) < self.max_per_hour:
            return 0.0
        # Need to wait until the oldest event falls out of the window
        sleep_for = _WINDOW - (now - self.events[0]) + 0.05
        if sleep_for > 0:
            time.sleep(sleep_for)
        now = time.time()
        cutoff = now - _WINDOW
        while self.events and self.events[0] < cutoff:
            self.events.popleft()
        return sleep_for if sleep_for > 0 else 0.0

    def record(self) -> None:
        self.events.append(time.time())
