from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import date, timedelta
from typing import Any

import fakeredis.aioredis
import httpx2
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

import aviasales_mcp.server as server_module
from aviasales_mcp.queue import STREAM_KEY, RedisJobQueue
from aviasales_mcp.worker import Worker


class AgentCycleApi:
    """Имитирует upstream и считает реальные обращения worker к каждому методу."""

    def __init__(self) -> None:
        self.calls = {
            "search_flights": 0,
            "analyze_price_calendar": 0,
            "live_search_flights": 0,
        }

    async def search_flights(self, request) -> dict[str, Any]:
        """Возвращает детерминированное предложение из Data API."""
        self.calls["search_flights"] += 1
        return {
            "source": "test-data-cache",
            "live_availability": False,
            "query": request.model_dump(mode="json"),
            "count": 1,
            "offers": [{"origin": request.origin, "destination": request.destination}],
        }

    async def analyze_calendar(self, request) -> dict[str, Any]:
        """Возвращает детерминированный календарь цен."""
        self.calls["analyze_price_calendar"] += 1
        return {
            "source": "test-calendar-cache",
            "live_availability": False,
            "query": request.model_dump(mode="json"),
            "days_with_prices": 1,
            "daily_prices": [{"date": f"{request.month}-15", "price": 5000}],
        }

    async def live_search_flights(self, request) -> dict[str, Any]:
        """Проверяет скрытый пользовательский контекст и возвращает live-предложение."""
        self.calls["live_search_flights"] += 1
        assert request.user_ip == "203.0.113.10"
        assert request.user_agent == "AgentBrowser/1.0"
        assert request.referer == "https://travel.example/flights"
        return {
            "source": "test-live-search",
            "live_availability": True,
            "complete": True,
            "query": request.model_dump(mode="json", exclude={"user_ip", "user_agent", "referer"}),
            "count": 1,
            "offers": [{"price": 6000, "currency": request.currency}],
        }


def _tool_payload(result) -> dict[str, Any]:
    """Извлекает структурированный словарь из MCP-ответа разных версий клиента."""
    structured = getattr(result, "structured_content", None)
    if structured is not None:
        return structured
    return json.loads(result.content[0].text)


async def _agent_call(
    session: ClientSession,
    worker: Worker,
    settings,
    name: str,
    arguments: dict[str, Any],
    *,
    expects_job: bool,
):
    """Имитирует вызов агента и при необходимости проводит задание через worker."""
    pending_call = asyncio.create_task(session.call_tool(name, arguments))
    if expects_job:
        async with asyncio.timeout(2):
            messages = []
            while not messages:
                messages = await worker.redis.xreadgroup(
                    settings.stream_consumer_group,
                    "agent-e2e",
                    {STREAM_KEY: ">"},
                    count=1,
                )
                if not messages:
                    await asyncio.sleep(0)
        message_id, fields = messages[0][1][0]
        await worker.process_job(fields["job"], message_id)
    return await pending_call


@pytest.mark.asyncio
async def test_ai_agent_full_cycle_for_all_three_tools(settings, monkeypatch) -> None:
    """Проверяет HTTP MCP, Redis Stream, worker, кэш и ответы всех инструментов."""
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    e2e_settings = replace(
        settings,
        job_timeout_seconds=5,
        live_search_timeout_seconds=5,
        worker_concurrency=1,
    )
    queue = RedisJobQueue(redis, e2e_settings)
    upstream = AgentCycleApi()
    worker = Worker(redis, e2e_settings, upstream)
    monkeypatch.setattr(server_module, "settings", e2e_settings)
    monkeypatch.setattr(server_module, "redis", redis)
    monkeypatch.setattr(server_module, "job_queue", queue)

    await worker.ensure_group()
    transport = httpx2.ASGITransport(app=server_module.app)
    headers = {
        "X-User-IP": "203.0.113.10",
        "X-User-Agent": "AgentBrowser/1.0",
        "X-User-Referer": "https://travel.example/flights",
    }
    try:
        async with server_module.app.router.lifespan_context(server_module.app):
            async with httpx2.AsyncClient(
                transport=transport,
                base_url="http://127.0.0.1:8000",
                headers=headers,
            ) as http_client:
                async with streamable_http_client(
                    "http://127.0.0.1:8000/mcp",
                    http_client=http_client,
                    terminate_on_close=False,
                ) as (read_stream, write_stream):
                    async with ClientSession(read_stream, write_stream) as session:
                        await session.initialize()
                        tools = await session.list_tools()
                        assert {tool.name for tool in tools.tools} == {
                            "search_flights",
                            "analyze_price_calendar",
                            "live_search_flights",
                        }

                        cached_args = {
                            "origin": "MOW",
                            "destination": "LED",
                            "depart_date": (date.today() + timedelta(days=10)).isoformat(),
                        }
                        cached_first = _tool_payload(
                            await _agent_call(
                                session,
                                worker,
                                e2e_settings,
                                "search_flights",
                                cached_args,
                                expects_job=True,
                            )
                        )
                        cached_second = _tool_payload(
                            await _agent_call(
                                session,
                                worker,
                                e2e_settings,
                                "search_flights",
                                cached_args,
                                expects_job=False,
                            )
                        )
                        assert cached_first["cache"] == "miss"
                        assert cached_second["cache"] == "hit"
                        assert cached_second["count"] == 1

                        calendar_args = {
                            "origin": "MOW",
                            "destination": "LED",
                            "month": (date.today() + timedelta(days=40)).strftime("%Y-%m"),
                        }
                        calendar_first = _tool_payload(
                            await _agent_call(
                                session,
                                worker,
                                e2e_settings,
                                "analyze_price_calendar",
                                calendar_args,
                                expects_job=True,
                            )
                        )
                        calendar_second = _tool_payload(
                            await _agent_call(
                                session,
                                worker,
                                e2e_settings,
                                "analyze_price_calendar",
                                calendar_args,
                                expects_job=False,
                            )
                        )
                        assert calendar_first["cache"] == "miss"
                        assert calendar_second["cache"] == "hit"
                        assert calendar_second["days_with_prices"] == 1

                        live_args = {
                            **cached_args,
                            "adults": 1,
                            "children": 0,
                            "infants": 0,
                        }
                        live_first = _tool_payload(
                            await _agent_call(
                                session,
                                worker,
                                e2e_settings,
                                "live_search_flights",
                                live_args,
                                expects_job=True,
                            )
                        )
                        live_second = _tool_payload(
                            await _agent_call(
                                session,
                                worker,
                                e2e_settings,
                                "live_search_flights",
                                live_args,
                                expects_job=True,
                            )
                        )
                        assert live_first["cache"] == "bypass"
                        assert live_second["cache"] == "bypass"
                        assert live_second["complete"] is True
                        assert "user_ip" not in live_second["query"]

        assert upstream.calls == {
            "search_flights": 1,
            "analyze_price_calendar": 1,
            "live_search_flights": 2,
        }
        assert await redis.xlen(STREAM_KEY) == 0
    finally:
        await redis.aclose()
