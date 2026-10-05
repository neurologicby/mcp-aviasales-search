from __future__ import annotations

import json

import httpx
import pytest

from aviasales_mcp.api import TravelpayoutsClient, UpstreamError
from aviasales_mcp.models import CalendarAnalysisRequest, FlightSearchRequest


def affiliate_response(body: dict) -> dict:
    return {
        "result": {
            "trs": body["trs"],
            "marker": body["marker"],
            "shorten": body["shorten"],
            "links": [
                {
                    "url": item["url"],
                    "code": "success",
                    "partner_url": f"https://aviasales.tp.st/test-{index}",
                }
                for index, item in enumerate(body["links"])
            ],
        },
        "code": "success",
        "status": 200,
    }


@pytest.mark.asyncio
async def test_search_uses_v3_and_returns_only_verified_partner_links(settings) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["x-access-token"] == "test-token"
        if request.url.path == "/aviasales/v3/prices_for_dates":
            assert request.url.params["departure_at"] == "2026-11-10"
            assert request.url.params["return_at"] == "2026-11-15"
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": [
                        {
                            "origin": "MOW",
                            "destination": "LED",
                            "origin_airport": "SVO",
                            "destination_airport": "LED",
                            "departure_at": "2026-11-10T08:00:00+03:00",
                            "return_at": "2026-11-15T16:00:00+03:00",
                            "price": 90,
                            "transfers": 0,
                            "link": "/MOW1011LED15111?offer=abc",
                        },
                        {
                            "origin": "MOW",
                            "destination": "LED",
                            "departure_at": "2026-11-11T08:00:00+03:00",
                            "return_at": "2026-11-15T16:00:00+03:00",
                            "price": 50,
                            "transfers": 0,
                            "link": "/wrong-date",
                        },
                    ],
                },
            )
        assert request.url.path == "/links/v1/create"
        body = json.loads(request.content)
        assert body["trs"] == 197987
        assert body["marker"] == 339296
        assert body["links"] == [
            {
                "url": "https://www.aviasales.ru/search/MOW1011LED15111?offer=abc",
                "sub_id": "mcp-flight-search",
            }
        ]
        return httpx.Response(200, json=affiliate_response(body))

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url=settings.api_base_url)
    api = TravelpayoutsClient(settings, http)
    result = await api.search_flights(
        FlightSearchRequest(
            origin="MOW",
            destination="LED",
            depart_date="2026-11-10",
            return_date="2026-11-15",
            direct_only=True,
        )
    )
    assert result["count"] == 1
    assert result["offers"][0]["partner_url"] == "https://aviasales.tp.st/test-0"
    assert result["offers"][0]["affiliate_verified"] is True
    assert "direct_url" not in result["offers"][0]
    await http.aclose()


@pytest.mark.asyncio
async def test_calendar_returns_verified_links_for_best_dates(settings) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/aviasales/v3/grouped_prices":
            assert request.url.params["group_by"] == "departure_at"
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": {
                        "2026-11-01": {
                            "price": 100,
                            "transfers": 0,
                            "link": "/MOW0111LED1?offer=a",
                        },
                        "2026-11-02": {
                            "price": 80,
                            "transfers": 1,
                            "link": "/MOW0211LED1?offer=b",
                        },
                    },
                },
            )
        body = json.loads(request.content)
        return httpx.Response(200, json=affiliate_response(body))

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url=settings.api_base_url)
    api = TravelpayoutsClient(settings, http)
    result = await api.analyze_calendar(
        CalendarAnalysisRequest(origin="MOW", destination="LED", month="2026-11")
    )
    assert result["days_with_prices"] == 2
    assert all(item["affiliate_verified"] for item in result["best_dates"])
    assert all(item["partner_url"].startswith("https://") for item in result["best_dates"])
    assert all("_direct_url" not in item for item in result["daily_prices"])
    await http.aclose()


@pytest.mark.asyncio
async def test_partner_link_is_rejected_if_api_does_not_verify_it(settings) -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "result": {
                    "trs": 197987,
                    "marker": 339296,
                    "links": [
                        {
                            "url": "https://www.aviasales.ru/search/MOWLED1",
                            "code": "failed",
                            "partner_url": "",
                        }
                    ],
                },
                "code": "success",
                "status": 200,
            },
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url=settings.api_base_url)
    api = TravelpayoutsClient(settings, http)
    with pytest.raises(UpstreamError, match="could not create every"):
        await api.create_partner_links(["https://www.aviasales.ru/search/MOWLED1"])
    await http.aclose()


@pytest.mark.asyncio
async def test_upstream_errors_are_safe_and_classified(settings) -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(429, text="secret provider details", headers={"Retry-After": "2"})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url=settings.api_base_url)
    api = TravelpayoutsClient(settings, http)
    with pytest.raises(UpstreamError, match="HTTP 429") as caught:
        await api.search_flights(
            FlightSearchRequest(origin="MOW", destination="LED", depart_date="2026-11-10")
        )
    assert caught.value.retryable is True
    assert caught.value.retry_after_seconds == 2
    assert "secret" not in str(caught.value)
    await http.aclose()
