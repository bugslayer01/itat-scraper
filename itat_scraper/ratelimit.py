"""Sliding-window rate limiter — cap completed operations per minute."""
from __future__ import annotations

import time
from collections import deque


class RateLimiter:
    """At most `max_per_minute` operations within any 60-second window."""

    def __init__(self, max_per_minute: int | None):
        self.max_per_minute = max_per_minute
        self.events: deque[float] = deque()

    def wait(self) -> float:
        """Block until another operation is allowed. Returns seconds slept."""
        if not self.max_per_minute or self.max_per_minute <= 0:
            return 0.0
        now = time.time()
        # Drop events older than 60s
        cutoff = now - 60.0
        while self.events and self.events[0] < cutoff:
            self.events.popleft()
        if len(self.events) < self.max_per_minute:
            return 0.0
        # Need to wait until the oldest event falls out of the window
        sleep_for = 60.0 - (now - self.events[0]) + 0.05
        if sleep_for > 0:
            time.sleep(sleep_for)
        # Purge again after sleeping
        now = time.time()
        cutoff = now - 60.0
        while self.events and self.events[0] < cutoff:
            self.events.popleft()
        return sleep_for if sleep_for > 0 else 0.0

    def record(self) -> None:
        self.events.append(time.time())
