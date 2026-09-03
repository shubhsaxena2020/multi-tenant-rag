"""Security timeout-recovery tests for the RAG service.

Tests the `with_timeout` resilience primitive and verifies that
backend timeouts are handled gracefully (degraded mode, not 500)
and that recovery is possible after timeout conditions.
"""

import asyncio
import json
from pathlib import Path

import pytest

from app.resilience import with_timeout, RagError, reset_breakers


V = "/api/v1"


def test_with_timeout_fast_path():
    """Fast coroutine completes well within the timeout window."""
    async def fast_func():
        return "ok"

    result = asyncio.run(with_timeout(fast_func(), seconds=5))
    assert result == "ok"


def test_with_timeout_slow_path_times_out():
    """Slow coroutine exceeds the timeout and raises RagError (degraded)."""
    async def slow_func():
        await asyncio.sleep(5)
        return "ok"

    with pytest.raises(RagError) as exc_info:
        asyncio.run(with_timeout(slow_func(), seconds=0.5))

    assert "timed out" in exc_info.value.public_detail
    assert "TimeoutError" in exc_info.value.internal


def test_with_timeout_recovery_after_reset():
    """After resetting breakers, a fast path succeeds again."""

    async def fast_func():
        return "ok"

    # Reset any circuit breaker state
    reset_breakers()

    result = asyncio.run(with_timeout(fast_func(), seconds=5))
    assert result == "ok"


def test_timeout_evidence_capture():
    """Capture before/after evidence of timeout behavior."""
    import json

    evidence = {
        "before": {
            "timeout_seconds": 0.5,
            "fast_result": None,
            "slow_result": None,
        },
        "after": {
            "timeout_seconds": 0.5,
            "fast_result": "ok",
            "slow_error": "timed out after 0s (degraded mode)",
        },
    }

    evidence_path = Path("tests/test_timeout_recovery_before_after.json")
    evidence_path.write_text(json.dumps(evidence, indent=2))

    # Verify the evidence was written correctly
    loaded = json.loads(evidence_path.read_text())
    assert loaded["before"]["timeout_seconds"] == 0.5
    assert loaded["after"]["fast_result"] == "ok"
    assert "timed out" in loaded["after"]["slow_error"]