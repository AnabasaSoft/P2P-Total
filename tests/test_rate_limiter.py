"""core/rate_limiter.py: cubo de fichas usado por Soulseek/DC++/G2/eMule."""

import time

import pytest

from core.config import Config
from core.rate_limiter import RateLimiter, apply_global_limits, global_download_limiter, global_upload_limiter


@pytest.mark.asyncio
async def test_unlimited_never_waits():
    limiter = RateLimiter(rate_bps=0)
    start = time.monotonic()
    await limiter.consume(10_000_000)
    assert time.monotonic() - start < 0.1


@pytest.mark.asyncio
async def test_consume_within_burst_does_not_wait():
    limiter = RateLimiter(rate_bps=1000)
    start = time.monotonic()
    await limiter.consume(1)  # cabe de sobra en el primer "tick" de tokens
    assert time.monotonic() - start < 0.1


@pytest.mark.asyncio
async def test_consume_beyond_rate_sleeps_roughly_the_expected_time():
    limiter = RateLimiter(rate_bps=1000)  # 1000 bytes/s
    start = time.monotonic()
    await limiter.consume(500)  # ~0.5s, ya que empieza sin fichas acumuladas
    elapsed = time.monotonic() - start
    assert 0.35 < elapsed < 1.0


def test_set_rate_clamps_negative_to_zero():
    limiter = RateLimiter(rate_bps=500)
    limiter.set_rate(-100)
    assert limiter.rate_bps == 0


def test_apply_global_limits_converts_kbps_to_bytes_per_second():
    config = Config(global_download_limit_kbps=100, global_upload_limit_kbps=50)
    apply_global_limits(config)
    try:
        assert global_download_limiter.rate_bps == 100 * 1024
        assert global_upload_limiter.rate_bps == 50 * 1024
    finally:
        # No dejar el estado global alterado para el resto de tests/la app.
        global_download_limiter.set_rate(0)
        global_upload_limiter.set_rate(0)
