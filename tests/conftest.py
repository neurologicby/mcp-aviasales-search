from __future__ import annotations

import pytest

from aviasales_mcp.config import Settings


@pytest.fixture
def settings() -> Settings:
    return Settings(
        travelpayouts_token="test-token",
        travelpayouts_trs=197987,
        travelpayouts_marker=339296,
        mcp_bearer_token="test-mcp-bearer-token-at-least-32-chars",
        redis_url="redis://unused/0",
        request_timeout_seconds=2,
        job_timeout_seconds=2,
        queue_max_length=10,
        cache_ttl_seconds=60,
        result_ttl_seconds=10,
        worker_concurrency=1,
        worker_max_attempts=2,
    )
