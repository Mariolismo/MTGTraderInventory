"""Token-bucket rate limiter for CardTrader API calls."""

from __future__ import annotations

import threading
import time


class RateLimiter:
    """Issue-rate limiter: acquire() blocks until a token is available.

    Capacity defaults to 1 second of budget so short bursts stay near the
    sustained RPS without exceeding the CardTrader 1s window badly.
    """

    def __init__(self, rate_per_second: float, capacity: float | None = None) -> None:
        if rate_per_second <= 0:
            raise ValueError("rate_per_second must be positive")
        self._rate = rate_per_second
        self._capacity = capacity if capacity is not None else max(1.0, rate_per_second)
        self._tokens = self._capacity
        self._updated_at = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self, tokens: float = 1.0) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                elapsed = now - self._updated_at
                self._updated_at = now
                self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)
                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return
                deficit = tokens - self._tokens
                wait_s = deficit / self._rate
            time.sleep(wait_s)
