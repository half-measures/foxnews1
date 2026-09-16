"""Thread-safe token-bucket rate limiter."""

import threading
import time


class RateLimiter:
    """Allow on average `rate` calls per second, with bursts up to `burst`.

    Call `acquire()` before each request; it blocks until a token is available.
    """

    def __init__(self, rate: float = 1.0, burst: int = 1):
        if rate <= 0:
            raise ValueError("rate must be > 0")
        if burst < 1:
            raise ValueError("burst must be >= 1")
        self.rate = rate
        self.burst = burst
        self._tokens = float(burst)
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def _refill(self) -> None:
        now = time.monotonic()
        self._tokens = min(self.burst, self._tokens + (now - self._last) * self.rate)
        self._last = now

    def acquire(self) -> None:
        while True:
            with self._lock:
                self._refill()
                if self._tokens >= 1:
                    self._tokens -= 1
                    return
                wait = (1 - self._tokens) / self.rate
            time.sleep(wait)

    def penalize(self, seconds: float) -> None:
        """Drain the bucket so the next acquire waits at least `seconds` (e.g. after a 429)."""
        with self._lock:
            self._refill()
            self._tokens = min(self._tokens, 1.0 - seconds * self.rate)
