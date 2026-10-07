from __future__ import annotations

import logging
import os
import secrets
import time
from typing import Literal

import uvicorn
from mcp.server import MCPServer
from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import AnyHttpUrl, ValidationError
from redis.asyncio import Redis
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response

from . import __version__
from .config import Settings
from .models import CalendarAnalysisRequest, FlightSearchRequest, LiveFlightSearchRequest
from .queue import JobTimeoutError, QueueBusyError, RedisJobQueue

logger = logging.getLogger(__name__)
settings = Settings.from_env()
redis = Redis.from_url(settings.redis_url, decode_responses=True)
job_queue = RedisJobQueue(redis, settings)


class StaticServiceTokenVerifier(TokenVerifier):
    async def verify_token(self, token: str) -> AccessToken | None:
        if not settings.mcp_bearer_token or not secrets.compare_digest(
            token, settings.mcp_bearer_token
        ):
            return None
        return AccessToken(
            token=token,
            client_id="configured-service-client",
            scopes=["flights:read"],
            resource=settings.mcp_resource_server_url,
            claims={"iss": settings.mcp_issuer_url},
        )


auth_kwargs = {}
if settings.mcp_bearer_token:
    auth_kwargs = {
        "token_verifier": StaticServiceTokenVerifier(),
        "auth": AuthSettings(
            issuer_url=AnyHttpUrl(settings.mcp_issuer_url),
            resource_server_url=AnyHttpUrl(settings.mcp_resource_server_url),
            required_scopes=["flights:read"],
            validate_token_resource=True,
        ),
    }

mcp = MCPServer(
    "Aviasales Flight Search",
    version=__version__,
    instructions=(
        "Use search_flights for cached matching flight-price options and "
        "analyze_price_calendar for price-calendar statistics. Use live_search_flights only "
        "when the caller forwards the real user's network context and live API access is enabled."
    ),
    **auth_kwargs,
)


async def _submit(kind: str, payload: dict) -> dict:
    try:
        return await job_queue.submit(kind, payload)  # type: ignore[arg-type]
    except QueueBusyError as exc:
        return {"ok": False, "error": {"code": "QUEUE_FULL", "message": str(exc)}}
    except JobTimeoutError as exc:
        return {"ok": False, "error": {"code": "TIMEOUT", "message": str(exc)}}
    except RuntimeError as exc:
        return {"ok": False, "error": {"code": "PROCESSING_FAILED", "message": str(exc)}}


@mcp.tool()
async def search_flights(
    origin: str,
    destination: str,
    depart_date: str,
    return_date: str | None = None,
    currency: Literal["RUB", "USD", "EUR"] = "RUB",
    trip_class: Literal["economy"] = "economy",
    direct_only: bool = False,
    limit: int = 10,
) -> dict:
    """Find matching cached flight-price options for an exact route and travel date(s).

    IATA codes are required. Results are indicative cached prices, not guaranteed live inventory.
    """
    try:
        request = FlightSearchRequest(
            origin=origin,
            destination=destination,
            depart_date=depart_date,
            return_date=return_date,
            currency=currency,
            trip_class=trip_class,
            direct_only=direct_only,
            limit=limit,
        )
    except ValidationError as exc:
        raise ToolError("Invalid flight search parameters") from exc
    return await _submit("search_flights", request.model_dump(mode="json"))


@mcp.tool()
async def analyze_price_calendar(
    origin: str,
    destination: str,
    month: str,
    return_month: str | None = None,
    calendar_type: Literal["departure_date", "return_date"] = "departure_date",
    trip_length: int | None = None,
    currency: Literal["RUB", "USD", "EUR"] = "RUB",
    direct_only: bool = False,
) -> dict:
    """Analyze cached daily flight prices for a YYYY-MM month.

    Returns min/max/average/median, trend, cheapest dates and normalized daily prices.
    """
    try:
        request = CalendarAnalysisRequest(
            origin=origin,
            destination=destination,
            month=month,
            return_month=return_month,
            calendar_type=calendar_type,
            trip_length=trip_length,
            currency=currency,
            direct_only=direct_only,
        )
    except ValidationError as exc:
        raise ToolError("Invalid calendar analysis parameters") from exc
    return await _submit("analyze_price_calendar", request.model_dump(mode="json"))


@mcp.tool()
async def live_search_flights(
    origin: str,
    destination: str,
    depart_date: str,
    ctx: Context,
    return_date: str | None = None,
    currency: Literal["RUB", "USD", "EUR"] = "RUB",
    trip_class: Literal["economy", "business", "first", "comfort"] = "economy",
    adults: int = 1,
    children: int = 0,
    infants: int = 0,
    direct_only: bool = False,
    limit: int = 10,
) -> dict:
    """Run an uncached real-time flight search using the end user's forwarded HTTP context.

    The caller must forward X-User-IP and the original browser User-Agent and Referer headers.
    Results may take up to 60 seconds. Booking links are intentionally not generated automatically.
    """
    headers = {key.lower(): value for key, value in (ctx.headers or {}).items()}
    user_ip = headers.get("x-user-ip", "").split(",", maxsplit=1)[0].strip()
    user_agent = headers.get("x-user-agent") or headers.get("user-agent", "")
    referer = headers.get("x-user-referer") or headers.get("referer", "")
    if not user_ip or not user_agent or not referer:
        raise ToolError(
            "Live search requires forwarded X-User-IP, User-Agent, and Referer headers"
        )
    try:
        request = LiveFlightSearchRequest(
            origin=origin,
            destination=destination,
            depart_date=depart_date,
            return_date=return_date,
            currency=currency,
            trip_class=trip_class,
            adults=adults,
            children=children,
            infants=infants,
            direct_only=direct_only,
            limit=limit,
            user_ip=user_ip,
            user_agent=user_agent,
            referer=referer,
        )
    except ValidationError as exc:
        raise ToolError("Invalid live flight search parameters or user context") from exc
    return await _submit("live_search_flights", request.model_dump(mode="json"))


@mcp.custom_route("/healthz", methods=["GET"])
async def health(_: Request) -> Response:
    return JSONResponse({"status": "ok", "version": __version__})


@mcp.custom_route("/readyz", methods=["GET"])
async def readiness(_: Request) -> Response:
    try:
        redis_ok = bool(await redis.ping())
        groups = await redis.xinfo_groups("aviasales:jobs")
        queue_ok = any(
            group.get("name") == settings.stream_consumer_group for group in groups
        )
        workers = int(
            await redis.zcount("aviasales:worker-heartbeats", time.time() - 15, "+inf")
        )
    except Exception:
        redis_ok = False
        queue_ok = False
        workers = 0
    provider_ok = settings.affiliate_configured
    auth_ok = bool(settings.mcp_bearer_token)
    ready = redis_ok and queue_ok and workers > 0 and provider_ok and auth_ok
    return JSONResponse(
        {
            "status": "ready" if ready else "not_ready",
            "redis": redis_ok,
            "queue": queue_ok,
            "workers": workers,
            "provider": provider_ok,
            "authentication": auth_ok,
        },
        status_code=200 if ready else 503,
    )


@mcp.custom_route("/metrics", methods=["GET"])
async def metrics(_: Request) -> Response:
    try:
        depth = await job_queue.depth()
        pending_summary = await redis.xpending(
            "aviasales:jobs", settings.stream_consumer_group
        )
        pending = int(pending_summary.get("pending", 0))
        circuit_open = int(bool(await redis.exists("aviasales:circuit:open")))
        workers = int(
            await redis.zcount("aviasales:worker-heartbeats", time.time() - 15, "+inf")
        )
    except Exception:
        return PlainTextResponse("# metrics temporarily unavailable\n", status_code=503)
    body = (
        "# TYPE aviasales_queue_depth gauge\n"
        f"aviasales_queue_depth {depth}\n"
        "# TYPE aviasales_queue_pending gauge\n"
        f"aviasales_queue_pending {pending}\n"
        "# TYPE aviasales_circuit_open gauge\n"
        f"aviasales_circuit_open {circuit_open}\n"
        "# TYPE aviasales_workers gauge\n"
        f"aviasales_workers {workers}\n"
    )
    return PlainTextResponse(body, media_type="text/plain; version=0.0.4")


app = mcp.streamable_http_app(
    json_response=True,
    stateless_http=True,
    max_sessions=1000,
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=list(settings.allowed_hosts),
        allowed_origins=list(settings.allowed_origins),
    ),
)


def main() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    uvicorn.run(
        app,
        host=os.getenv("HOST", "127.0.0.1"),
        port=int(os.getenv("PORT", "8000")),
        proxy_headers=True,
    )


if __name__ == "__main__":
    main()
