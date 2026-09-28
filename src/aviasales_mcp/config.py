from __future__ import annotations

import os
from dataclasses import dataclass


def _int(name: str, default: int, *, minimum: int = 1) -> int:
    raw = os.getenv(name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    return value


def _required_int(name: str) -> int | None:
    raw = os.getenv(name, "").strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if value < 1:
        raise ValueError(f"{name} must be >= 1")
    return value


def _csv(name: str, default: str) -> tuple[str, ...]:
    return tuple(value.strip() for value in os.getenv(name, default).split(",") if value.strip())


def _secret(name: str, *, minimum_length: int = 32) -> str:
    value = os.getenv(name, "").strip()
    if value and len(value) < minimum_length:
        raise ValueError(f"{name} must contain at least {minimum_length} characters")
    return value


@dataclass(frozen=True, slots=True)
class Settings:
    travelpayouts_token: str
    travelpayouts_trs: int | None = None
    travelpayouts_marker: int | None = None
    affiliate_sub_id: str = "mcp-flight-search"
    affiliate_shorten: bool = False
    redis_url: str = "redis://localhost:6379/0"
    api_base_url: str = "https://api.travelpayouts.com"
    request_timeout_seconds: int = 8
    job_timeout_seconds: int = 25
    queue_max_length: int = 10_000
    cache_ttl_seconds: int = 300
    result_ttl_seconds: int = 60
    worker_concurrency: int = 20
    worker_max_attempts: int = 3
    stream_consumer_group: str = "aviasales-workers"
    stream_lease_seconds: int = 35
    partner_links_per_minute: int = 90
    mcp_requests_per_minute: int = 600
    circuit_breaker_failures: int = 5
    circuit_breaker_open_seconds: int = 30
    mcp_bearer_token: str = ""
    mcp_issuer_url: str = "https://auth.invalid"
    mcp_resource_server_url: str = "http://localhost:8000/mcp"
    allowed_hosts: tuple[str, ...] = ("127.0.0.1:*", "localhost:*", "[::1]:*")
    allowed_origins: tuple[str, ...] = (
        "http://127.0.0.1:*",
        "http://localhost:*",
        "http://[::1]:*",
    )

    @property
    def affiliate_configured(self) -> bool:
        return bool(
            self.travelpayouts_token
            and self.travelpayouts_trs
            and self.travelpayouts_marker
        )

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            travelpayouts_token=os.getenv("TRAVELPAYOUTS_TOKEN", "").strip(),
            travelpayouts_trs=_required_int("TRAVELPAYOUTS_TRS"),
            travelpayouts_marker=_required_int("TRAVELPAYOUTS_MARKER"),
            affiliate_sub_id=os.getenv("AFFILIATE_SUB_ID", "mcp-flight-search").strip(),
            affiliate_shorten=os.getenv("AFFILIATE_SHORTEN", "false").lower() == "true",
            redis_url=os.getenv("REDIS_URL", "redis://localhost:6379/0").strip(),
            api_base_url=os.getenv(
                "TRAVELPAYOUTS_API_BASE_URL", "https://api.travelpayouts.com"
            ).rstrip("/"),
            request_timeout_seconds=_int("REQUEST_TIMEOUT_SECONDS", 8),
            job_timeout_seconds=_int("JOB_TIMEOUT_SECONDS", 25),
            queue_max_length=_int("QUEUE_MAX_LENGTH", 10_000),
            cache_ttl_seconds=_int("CACHE_TTL_SECONDS", 300),
            result_ttl_seconds=_int("RESULT_TTL_SECONDS", 60),
            worker_concurrency=_int("WORKER_CONCURRENCY", 20),
            worker_max_attempts=_int("WORKER_MAX_ATTEMPTS", 3),
            stream_consumer_group=os.getenv(
                "STREAM_CONSUMER_GROUP", "aviasales-workers"
            ).strip(),
            stream_lease_seconds=_int("STREAM_LEASE_SECONDS", 35),
            partner_links_per_minute=_int("PARTNER_LINKS_PER_MINUTE", 90),
            mcp_requests_per_minute=_int("MCP_REQUESTS_PER_MINUTE", 600),
            circuit_breaker_failures=_int("CIRCUIT_BREAKER_FAILURES", 5),
            circuit_breaker_open_seconds=_int("CIRCUIT_BREAKER_OPEN_SECONDS", 30),
            mcp_bearer_token=_secret("MCP_BEARER_TOKEN"),
            mcp_issuer_url=os.getenv("MCP_ISSUER_URL", "https://auth.invalid").strip(),
            mcp_resource_server_url=os.getenv(
                "MCP_RESOURCE_SERVER_URL", "http://localhost:8000/mcp"
            ).strip(),
            allowed_hosts=_csv(
                "MCP_ALLOWED_HOSTS", "127.0.0.1:*,localhost:*,[::1]:*"
            ),
            allowed_origins=_csv(
                "MCP_ALLOWED_ORIGINS",
                "http://127.0.0.1:*,http://localhost:*,http://[::1]:*",
            ),
        )
