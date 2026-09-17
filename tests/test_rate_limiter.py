import threading
import time

import pytest

from foxcomments.rate_limiter import RateLimiter


def test_burst_is_immediate_then_spaced_at_rate(clock):
    rl = RateLimiter(rate=4, burst=2)
    times = []
    for _ in range(6):
        rl.acquire()
        times.append(round(clock.now - 1000, 3))
    assert times == [0, 0, 0.25, 0.5, 0.75, 1.0]


def test_idle_time_refills_but_never_beyond_burst(clock):
    rl = RateLimiter(rate=1, burst=3)
    for _ in range(3):
        rl.acquire()
    clock.now += 100  # long idle
    start = clock.now
    for _ in range(3):
        rl.acquire()
    assert clock.now == start  # burst of 3 available again
    rl.acquire()
    assert clock.now == pytest.approx(start + 1)  # 4th waits: bucket capped at 3


def test_penalize_delays_next_acquire_by_exactly_that_long(clock):
    rl = RateLimiter(rate=2, burst=1)
    rl.acquire()
    clock.now += 10  # bucket full again
    rl.penalize(5)
    start = clock.now
    rl.acquire()
    assert clock.now - start == pytest.approx(5)


def test_penalize_does_not_shorten_an_existing_wait(clock):
    rl = RateLimiter(rate=1, burst=1)
    rl.acquire()
    rl.penalize(0.1)  # already needs ~1s for the next token
    start = clock.now
    rl.acquire()
    assert clock.now - start == pytest.approx(1)


@pytest.mark.parametrize("kwargs", [{"rate": 0}, {"rate": -1}, {"rate": 1, "burst": 0}])
def test_rejects_invalid_settings(kwargs):
    with pytest.raises(ValueError):
        RateLimiter(**kwargs)


def test_thread_safe_under_real_time():
    rl = RateLimiter(rate=50, burst=1)
    stamps = []
    lock = threading.Lock()

    def worker():
        for _ in range(4):
            rl.acquire()
            with lock:
                stamps.append(time.monotonic())

    threads = [threading.Thread(target=worker) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # 20 acquires at 50/s with no burst: the first is free, the other 19 need >= 19/50 s.
    assert len(stamps) == 20
    assert max(stamps) - min(stamps) >= 19 / 50 * 0.9
