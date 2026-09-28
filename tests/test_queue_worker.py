from __future__ import annotations

import asyncio
import json
import time
from dataclasses import replace
from datetime import date, timedelta

import fakeredis.aioredis
import pytest

from aviasales_mcp.api import UpstreamError
from aviasales_mcp.queue import STREAM_KEY, QueueBusyError, RedisJobQueue
from aviasales_mcp.worker import Worker


class StubApi:
    async def search_flights(self, request):
        return {"offers": [{"origin": request.origin}], "count": 1}

    async def analyze_calendar(self, request):
        return {"days_with_prices": 2, "origin": request.origin}


@pytest.mark.asyncio
async def test_queue_worker_roundtrip_and_cache(settings) -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    queue = RedisJobQueue(redis, settings)
    worker = Worker(redis, settings, StubApi())
    payload = {
        "origin": "MOW",
        "destination": "LED",
        "depart_date": "2026-11-10",
        "return_date": None,
        "currency": "RUB",
        "trip_class": "economy",
        "direct_only": False,
        "limit": 10,
    }

    await worker.ensure_group()
    submitted = asyncio.create_task(queue.submit("search_flights", payload))
    await asyncio.sleep(0)
    messages = await redis.xreadgroup(
        settings.stream_consumer_group, "test", {STREAM_KEY: ">"}, count=1
    )
    message_id, fields = messages[0][1][0]
    await worker.process_job(fields["job"], message_id)
    first = await submitted
    second = await queue.submit("search_flights", payload)

    assert first["cache"] == "miss"
    assert first["offers"][0]["origin"] == "MOW"
    assert second["cache"] == "hit"
    assert await redis.xlen(STREAM_KEY) == 0
    await redis.aclose()


@pytest.mark.asyncio
async def test_queue_backpressure(settings) -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    settings = replace(settings, queue_max_length=1)
    queue = RedisJobQueue(redis, settings)
    await redis.xadd(STREAM_KEY, {"job": "occupied"})
    with pytest.raises(QueueBusyError):
        await queue.submit("search_flights", {"x": 1})
    await redis.aclose()


@pytest.mark.asyncio
async def test_distributed_request_rate_limit(settings) -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    settings = replace(settings, mcp_requests_per_minute=1)
    queue = RedisJobQueue(redis, settings)
    payload = {"cached": True}
    await redis.set(queue.cache_key("search_flights", payload), '{"count":0,"offers":[]}')
    assert (await queue.submit("search_flights", payload))["cache"] == "hit"
    with pytest.raises(QueueBusyError, match="rate limit"):
        await queue.submit("search_flights", payload)
    await redis.aclose()


@pytest.mark.asyncio
async def test_pending_stream_message_can_be_reclaimed(settings) -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    worker = Worker(redis, settings, StubApi())
    await worker.ensure_group()
    message_id = await redis.xadd(STREAM_KEY, {"job": "crash-simulation"})
    await redis.xreadgroup(
        settings.stream_consumer_group, "dead-worker", {STREAM_KEY: ">"}, count=1
    )
    claimed = await redis.xautoclaim(
        STREAM_KEY,
        settings.stream_consumer_group,
        "replacement-worker",
        min_idle_time=0,
        start_id="0-0",
        count=1,
    )
    assert claimed[1][0][0] == message_id
    await redis.aclose()


@pytest.mark.asyncio
async def test_expired_job_is_rejected_and_acknowledged(settings) -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    worker = Worker(redis, settings, StubApi())
    await worker.ensure_group()
    job = {
        "id": "expired-job",
        "kind": "search_flights",
        "payload": {},
        "cache_key": "unused",
        "deadline": time.time() - 1,
    }
    message_id = await redis.xadd(STREAM_KEY, {"job": json.dumps(job)})
    await redis.xreadgroup(settings.stream_consumer_group, "test", {STREAM_KEY: ">"}, count=1)
    await worker.process_job(json.dumps(job), message_id)
    result = await redis.blpop("aviasales:result:expired-job", timeout=1)
    assert json.loads(result[1]) == {"ok": False, "error": "job expired in queue"}
    assert await redis.xlen(STREAM_KEY) == 0
    await redis.aclose()


@pytest.mark.asyncio
async def test_partner_link_quota_and_circuit_breaker_are_distributed(settings) -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    settings = replace(settings, partner_links_per_minute=1, circuit_breaker_failures=2)
    worker = Worker(redis, settings, StubApi())
    await worker.acquire_partner_link_quota()
    with pytest.raises(UpstreamError, match="rate limit"):
        await worker.acquire_partner_link_quota()
    assert await worker._circuit_is_open() is False
    await worker._record_upstream_failure()
    await worker._record_upstream_failure()
    assert await worker._circuit_is_open() is True
    await worker._record_upstream_success()
    assert await worker._circuit_is_open() is False
    await redis.aclose()


@pytest.mark.asyncio
async def test_queue_load_smoke_100_concurrent_jobs(settings) -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    settings = replace(settings, job_timeout_seconds=10, queue_max_length=200, worker_concurrency=8)
    queue = RedisJobQueue(redis, settings)
    worker = Worker(redis, settings, StubApi())
    await worker.ensure_group()

    async def submit(index: int) -> dict:
        payload = {
            "origin": "MOW",
            "destination": "LED",
            "depart_date": (date(2026, 10, 1) + timedelta(days=index)).isoformat(),
            "return_date": None,
            "currency": "RUB",
            "trip_class": "economy",
            "direct_only": False,
            "limit": 10,
        }
        return await queue.submit("search_flights", payload)

    submitted = [asyncio.create_task(submit(index)) for index in range(100)]
    await asyncio.sleep(0.1)
    messages = await redis.xreadgroup(
        settings.stream_consumer_group, "load-test", {STREAM_KEY: ">"}, count=100
    )
    await asyncio.gather(
        *(
            worker.process_job(fields["job"], message_id)
            for _, batch in messages
            for message_id, fields in batch
        )
    )
    results = await asyncio.gather(*submitted)

    assert len(results) == 100
    assert all(result["count"] == 1 for result in results)
    assert await redis.xlen(STREAM_KEY) == 0
    await redis.aclose()
