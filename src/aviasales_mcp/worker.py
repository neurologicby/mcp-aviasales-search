from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import socket
import time
from functools import partial
from typing import Any

from pydantic import ValidationError
from redis.asyncio import Redis
from redis.exceptions import ResponseError

from .api import TravelpayoutsClient, UpstreamError, execute_with_retries
from .config import Settings
from .models import CalendarAnalysisRequest, FlightSearchRequest
from .queue import DEAD_LETTER_STREAM, STREAM_KEY

logger = logging.getLogger(__name__)


class Worker:
    def __init__(self, redis: Redis, settings: Settings, api: TravelpayoutsClient) -> None:
        self.redis = redis
        self.settings = settings
        self.api = api
        self.stopping = asyncio.Event()
        self.consumer_prefix = f"{socket.gethostname()}-{os.getpid()}"

    async def ensure_group(self) -> None:
        try:
            await self.redis.xgroup_create(
                STREAM_KEY, self.settings.stream_consumer_group, id="0", mkstream=True
            )
        except ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    async def acquire_partner_link_quota(self) -> None:
        window = int(time.time() // 60)
        key = f"aviasales:rate:partner-links:{self.settings.travelpayouts_marker}:{window}"
        count = await self.redis.incr(key)
        if count == 1:
            await self.redis.expire(key, 70)
        if count > self.settings.partner_links_per_minute:
            retry_after = 60 - (time.time() % 60)
            raise UpstreamError(
                "Travelpayouts partner-link rate limit reached",
                retryable=True,
                retry_after_seconds=retry_after,
            )

    async def _publish(self, job_id: str, result: dict[str, Any]) -> None:
        key = f"aviasales:result:{job_id}"
        async with self.redis.pipeline(transaction=True) as pipeline:
            pipeline.lpush(key, json.dumps(result, separators=(",", ":")))
            pipeline.expire(key, self.settings.result_ttl_seconds)
            await pipeline.execute()

    async def _ack(self, message_id: str) -> None:
        async with self.redis.pipeline(transaction=True) as pipeline:
            pipeline.xack(STREAM_KEY, self.settings.stream_consumer_group, message_id)
            pipeline.xdel(STREAM_KEY, message_id)
            await pipeline.execute()

    async def _circuit_is_open(self) -> bool:
        return bool(await self.redis.exists("aviasales:circuit:open"))

    async def _record_upstream_failure(self) -> None:
        failures = await self.redis.incr("aviasales:circuit:failures")
        await self.redis.expire("aviasales:circuit:failures", 60)
        if failures >= self.settings.circuit_breaker_failures:
            await self.redis.set(
                "aviasales:circuit:open",
                "1",
                ex=self.settings.circuit_breaker_open_seconds,
            )

    async def _record_upstream_success(self) -> None:
        await self.redis.delete("aviasales:circuit:failures", "aviasales:circuit:open")

    async def process_job(self, raw: str | bytes, message_id: str) -> None:
        job_id = "unknown"
        try:
            job = json.loads(raw)
            job_id = job["id"]
            remaining = float(job["deadline"]) - time.time()
            if remaining <= 0:
                await self._publish(job_id, {"ok": False, "error": "job expired in queue"})
                await self._ack(message_id)
                return
            if await self._circuit_is_open():
                await self._publish(
                    job_id,
                    {"ok": False, "error": "Travelpayouts circuit breaker is open"},
                )
                await self._ack(message_id)
                return
            kind = job["kind"]
            if kind == "search_flights":
                request = FlightSearchRequest.model_validate(job["payload"])
                operation = partial(self.api.search_flights, request)
            elif kind == "analyze_price_calendar":
                request = CalendarAnalysisRequest.model_validate(job["payload"])
                operation = partial(self.api.analyze_calendar, request)
            else:
                await self._publish(job_id, {"ok": False, "error": "unknown job type"})
                await self._ack(message_id)
                return
            async with asyncio.timeout(remaining):
                result = await execute_with_retries(
                    operation, max_attempts=self.settings.worker_max_attempts
                )
            await self._record_upstream_success()
            encoded = json.dumps(result, separators=(",", ":"))
            async with self.redis.pipeline(transaction=True) as pipeline:
                pipeline.set(job["cache_key"], encoded, ex=self.settings.cache_ttl_seconds)
                pipeline.lpush(
                    f"aviasales:result:{job_id}",
                    json.dumps({"ok": True, "data": result}, separators=(",", ":")),
                )
                pipeline.expire(
                    f"aviasales:result:{job_id}", self.settings.result_ttl_seconds
                )
                pipeline.xack(
                    STREAM_KEY, self.settings.stream_consumer_group, message_id
                )
                pipeline.xdel(STREAM_KEY, message_id)
                pipeline.delete(f"aviasales:attempts:{message_id}")
                await pipeline.execute()
        except TimeoutError:
            await self._record_upstream_failure()
            await self._publish(job_id, {"ok": False, "error": "job execution timed out"})
            await self._ack(message_id)
        except (KeyError, TypeError, ValueError, ValidationError) as exc:
            logger.warning("Rejected malformed job %s: %s", job_id, type(exc).__name__)
            await self._publish(job_id, {"ok": False, "error": "invalid queued job"})
            await self._ack(message_id)
        except UpstreamError as exc:
            if exc.retryable:
                await self._record_upstream_failure()
            logger.warning("Upstream request failed for job %s: %s", job_id, exc)
            await self._publish(job_id, {"ok": False, "error": str(exc)})
            await self._ack(message_id)
        except Exception:
            logger.exception("Unexpected worker failure for job %s", job_id)
            attempts_key = f"aviasales:attempts:{message_id}"
            attempts = await self.redis.incr(attempts_key)
            await self.redis.expire(attempts_key, 3600)
            if attempts >= self.settings.worker_max_attempts:
                await self._publish(
                    job_id, {"ok": False, "error": "internal worker error"}
                )
                await self.redis.xadd(
                    DEAD_LETTER_STREAM,
                    {"message_id": message_id, "job": raw, "error": "internal worker error"},
                    maxlen=1000,
                    approximate=True,
                )
                await self._ack(message_id)

    async def run_slot(self, slot: int) -> None:
        consumer = f"{self.consumer_prefix}-{slot}"
        logger.info("Worker slot %d started", slot)
        while not self.stopping.is_set():
            messages = await self.redis.xreadgroup(
                self.settings.stream_consumer_group,
                consumer,
                {STREAM_KEY: ">"},
                count=1,
                block=1000,
            )
            for _, batch in messages:
                for message_id, fields in batch:
                    await self.process_job(fields["job"], message_id)

    async def reclaim_stale(self) -> None:
        consumer = f"{self.consumer_prefix}-reclaimer"
        interval = max(1, self.settings.stream_lease_seconds // 2)
        while not self.stopping.is_set():
            await asyncio.sleep(interval)
            claimed = await self.redis.xautoclaim(
                STREAM_KEY,
                self.settings.stream_consumer_group,
                consumer,
                min_idle_time=self.settings.stream_lease_seconds * 1000,
                start_id="0-0",
                count=10,
            )
            for message_id, fields in claimed[1]:
                logger.warning("Reclaimed stale queue message %s", message_id)
                await self.process_job(fields["job"], message_id)

    async def heartbeat(self) -> None:
        while not self.stopping.is_set():
            await self.redis.zadd(
                "aviasales:worker-heartbeats", {self.consumer_prefix: time.time()}
            )
            await asyncio.sleep(5)

    async def run(self) -> None:
        await self.ensure_group()
        tasks = [
            asyncio.create_task(self.run_slot(slot), name=f"worker-{slot}")
            for slot in range(self.settings.worker_concurrency)
        ]
        tasks.append(asyncio.create_task(self.reclaim_stale(), name="reclaimer"))
        tasks.append(asyncio.create_task(self.heartbeat(), name="heartbeat"))
        await self.stopping.wait()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await self.redis.zrem("aviasales:worker-heartbeats", self.consumer_prefix)

    def stop(self) -> None:
        self.stopping.set()


async def async_main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    settings = Settings.from_env()
    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    worker: Worker | None = None

    async def partner_limiter() -> None:
        if worker is None:
            raise RuntimeError("worker is not initialized")
        await worker.acquire_partner_link_quota()

    api = TravelpayoutsClient(settings, partner_link_limiter=partner_limiter)
    worker = Worker(redis, settings, api)
    loop = asyncio.get_running_loop()
    for name in ("SIGINT", "SIGTERM"):
        sig = getattr(signal, name, None)
        if sig is not None:
            try:
                loop.add_signal_handler(sig, worker.stop)
            except NotImplementedError:
                pass
    try:
        await worker.run()
    finally:
        await api.close()
        await redis.aclose()


def main() -> None:
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
