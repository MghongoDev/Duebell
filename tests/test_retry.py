"""Retry helper tests (issue #18)."""

import httpx
import pytest

from app.retry import RetryableFailure, call_with_retries


def test_retries_with_backoff_and_succeeds():
    calls = []
    delays = []

    def operation():
        calls.append(1)
        if len(calls) < 3:
            raise RetryableFailure("temporary")
        return "ok"

    result = call_with_retries(operation, attempts=3, base_delay=0.5, sleep=delays.append)
    assert result == "ok"
    assert len(calls) == 3
    assert delays == [0.5, 1.0]


def test_gives_up_after_the_last_attempt():
    calls = []

    def operation():
        calls.append(1)
        raise RetryableFailure("still down")

    with pytest.raises(RetryableFailure):
        call_with_retries(operation, attempts=2, sleep=lambda _: None)
    assert len(calls) == 2


def test_does_not_retry_other_errors():
    calls = []

    def operation():
        calls.append(1)
        raise ValueError("bad input")

    with pytest.raises(ValueError):
        call_with_retries(operation, attempts=3, sleep=lambda _: None)
    assert len(calls) == 1


def test_retries_a_timeout():
    calls = []

    def operation():
        calls.append(1)
        if len(calls) == 1:
            raise httpx.ReadTimeout("slow")
        return "ok"

    assert call_with_retries(operation, attempts=2, sleep=lambda _: None) == "ok"


def test_rejects_a_zero_attempt_budget():
    with pytest.raises(ValueError):
        call_with_retries(lambda: "ok", attempts=0)
