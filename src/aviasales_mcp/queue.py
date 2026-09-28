from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import dataclass
from typing import Any, Literal

from redis.asyncio import Redis
from redis.exceptions import WatchError

from .config import Settings

JobType = Literal["search_flights", "analyze_price_calendar"]
STREAM_KEY = "aviasales:jobs"
DEAD_LETTER_STREAM = "aviasales:jobs:dead-letter"


class QueueBusyError(RuntimeError):
    pass


class JobTimeoutError(RuntimeError):
    pass


@dataclass(slots=True)
class RedisJobQueue:
    redis: Redis
    settings: Settings

    @staticmethod
    def cache_key(kind: JobType, payload: dict[str, Any]) -> str:
        canonical = json.dumps(
            {"kind": kind, "payload": payload}, sort_keys=True, separators=(",", ":")
        )
        digest = hashlib.sha256(canonical.encode()).hexdigest()
        return f"aviasales:cache:{digest}"

    async def submit(self, kind: JobType, payload: dict[str, Any]) -> dict[str, Any]:
        rate_window = int(time.time() // 60)
        rate_key = f"aviasales:rate:mcp:{rate_window}"
        request_count = await self.redis.incr(rate_key)
        if request_count == 1:
            await self.redis.expire(rate_key, 70)
        if request_count > self.settings.mcp_requests_per_minute:
            raise QueueBusyError("MCP request rate limit reached; retry later")

        cache_key = self.cache_key(kind, payload)
        cached = await self.redis.get(cache_key)
        if cached:
            result = json.loads(cached)
            result["cache"] = "hit"
            return result

        job_id = uuid.uuid4().hex
        result_key = f"aviasales:result:{job_id}"
        job = {
            "id": job_id,
            "kind": kind,
            "payload": payload,
            "cache_key": cache_key,
            "deadline": time.time() + self.settings.job_timeout_seconds,
        }
        encoded_job = json.dumps(job, separators=(",", ":"))
        while True:
            async with self.redis.pipeline(transaction=True) as pipeline:
                try:
                    await pipeline.watch(STREAM_KEY)
                    queue_length = await pipeline.xlen(STREAM_KEY)
                    if queue_length >= self.settings.queue_max_length:
                        await pipeline.reset()
                        raise QueueBusyError("request queue is full; retry later")
                    pipeline.multi()
                    pipeline.xadd(STREAM_KEY, {"job": encoded_job})
                    await pipeline.execute()
                    break
                except WatchError:
                    continue

        response = await self.redis.blpop(result_key, timeout=self.settings.job_timeout_seconds)
        if response is None:
            raise JobTimeoutError("queued request timed out")
        result = json.loads(response[1])
        if result.get("ok") is not True:
            raise RuntimeError(result.get("error", "worker failed"))
        value = result["data"]
        value["cache"] = "miss"
        return value

    async def depth(self) -> int:
        return int(await self.redis.xlen(STREAM_KEY))
