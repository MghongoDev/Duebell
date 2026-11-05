"""Retry helper for outbound network calls (issue #18).

PayPal and the model endpoint are remote. A short, bounded retry with backoff
keeps the demo working through a transient timeout without hiding a real error.
Only timeouts, transport errors, and explicitly retryable failures are retried.
"""

from __future__ import annotations

import time
from typing import Callable, TypeVar

import httpx

T = TypeVar("T")

RETRYABLE = (httpx.TimeoutException, httpx.TransportError)


class RetryableFailure(Exception):
    """A call failed in a way that is safe to retry."""


def call_with_retries(
    operation: Callable[[], T],
    *,
    attempts: int = 3,
    base_delay: float = 0.5,
    max_delay: float = 4.0,
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    if attempts < 1:
        raise ValueError("attempts must be at least 1")
    delay = base_delay
    for attempt in range(attempts):
        try:
            return operation()
        except RETRYABLE + (RetryableFailure,):
            if attempt == attempts - 1:
                raise
            sleep(delay)
            delay = min(delay * 2, max_delay)
    raise RuntimeError("retry loop ended without a result")
