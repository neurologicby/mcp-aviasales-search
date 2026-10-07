from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

import httpx

from .config import Settings
from .models import (
    CalendarAnalysisRequest,
    FlightSearchRequest,
    LiveFlightSearchRequest,
    analyze_calendar_entries,
)


class UpstreamError(RuntimeError):
    """A safe upstream error that may be returned to an MCP client."""

    def __init__(
        self, message: str, *, retryable: bool, retry_after_seconds: float | None = None
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.retry_after_seconds = retry_after_seconds


class TravelpayoutsClient:
    def __init__(
        self,
        settings: Settings,
        client: httpx.AsyncClient | None = None,
        partner_link_limiter: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self.settings = settings
        self._owns_client = client is None
        self._partner_link_limiter = partner_link_limiter
        self.client = client or httpx.AsyncClient(
            base_url=settings.api_base_url,
            timeout=httpx.Timeout(settings.request_timeout_seconds),
            limits=httpx.Limits(max_connections=200, max_keepalive_connections=100),
            headers={"Accept": "application/json", "Accept-Encoding": "gzip, deflate"},
        )

    async def close(self) -> None:
        if self._owns_client:
            await self.client.aclose()

    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not self.settings.travelpayouts_token:
            raise UpstreamError("TRAVELPAYOUTS_TOKEN is not configured", retryable=False)
        try:
            response = await self.client.request(
                method,
                path,
                params=params,
                json=json_body,
                headers={"X-Access-Token": self.settings.travelpayouts_token},
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise UpstreamError(
                "Travelpayouts is temporarily unavailable", retryable=True
            ) from exc
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            retryable = status == 429 or status >= 500
            retry_after: float | None = None
            if status == 429:
                try:
                    retry_after = float(exc.response.headers.get("Retry-After", ""))
                except ValueError:
                    retry_after = None
            raise UpstreamError(
                f"Travelpayouts returned HTTP {status}",
                retryable=retryable,
                retry_after_seconds=retry_after,
            ) from exc
        except ValueError as exc:
            raise UpstreamError("Travelpayouts returned invalid JSON", retryable=True) from exc
        if not isinstance(payload, dict):
            raise UpstreamError("Travelpayouts returned an invalid response", retryable=True)
        return payload

    async def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        payload = await self._request_json("GET", path, params=params)
        if payload.get("success") is False:
            message = payload.get("error")
            safe_message = (
                message if isinstance(message, str) and len(message) <= 200 else "unknown error"
            )
            raise UpstreamError(
                f"Travelpayouts rejected the request: {safe_message}", retryable=False
            )
        return payload

    @staticmethod
    def _direct_aviasales_url(link: Any) -> str | None:
        if not isinstance(link, str) or not link.startswith("/") or link.startswith("//"):
            return None
        return f"https://www.aviasales.ru/search{link}"

    async def create_partner_links(self, direct_urls: list[str]) -> dict[str, str]:
        if not direct_urls:
            return {}
        if len(direct_urls) > 10:
            raise UpstreamError(
                "partner-link request exceeds the documented 10-link limit", retryable=False
            )
        if not self.settings.affiliate_configured:
            raise UpstreamError(
                "Travelpayouts affiliate settings are not configured", retryable=False
            )
        if self._partner_link_limiter is not None:
            await self._partner_link_limiter()
        links: list[dict[str, str]] = []
        for url in direct_urls:
            item = {"url": url}
            if self.settings.affiliate_sub_id:
                item["sub_id"] = self.settings.affiliate_sub_id
            links.append(item)
        payload = await self._request_json(
            "POST",
            "/links/v1/create",
            json_body={
                "trs": self.settings.travelpayouts_trs,
                "marker": self.settings.travelpayouts_marker,
                "shorten": self.settings.affiliate_shorten,
                "links": links,
            },
        )
        result = payload.get("result")
        if (
            payload.get("code") != "success"
            or payload.get("status") != 200
            or not isinstance(result, dict)
            or result.get("trs") != self.settings.travelpayouts_trs
            or result.get("marker") != self.settings.travelpayouts_marker
        ):
            raise UpstreamError(
                "Travelpayouts did not verify the affiliate-link response", retryable=False
            )
        converted = result.get("links")
        if not isinstance(converted, list):
            raise UpstreamError("Travelpayouts returned malformed partner links", retryable=True)
        partner_links: dict[str, str] = {}
        for item in converted:
            if not isinstance(item, dict):
                continue
            source = item.get("url")
            partner_url = item.get("partner_url")
            if (
                item.get("code") == "success"
                and isinstance(source, str)
                and isinstance(partner_url, str)
                and partner_url.startswith("https://")
            ):
                partner_links[source] = partner_url
        if set(partner_links) != set(direct_urls):
            raise UpstreamError(
                "Travelpayouts could not create every requested partner link", retryable=False
            )
        return partner_links

    async def search_flights(self, request: FlightSearchRequest) -> dict[str, Any]:
        params: dict[str, Any] = {
            "origin": request.origin,
            "destination": request.destination,
            "departure_at": request.depart_date.isoformat(),
            "one_way": str(request.return_date is None).lower(),
            "direct": str(request.direct_only).lower(),
            "currency": request.currency.lower(),
            "market": "ru",
            "page": 1,
            "limit": request.limit,
            "sorting": "price",
            "unique": "false",
        }
        if request.return_date is not None:
            params["return_at"] = request.return_date.isoformat()
        payload = await self._get("/aviasales/v3/prices_for_dates", params)
        raw = payload.get("data", [])
        if not isinstance(raw, list):
            raise UpstreamError("Travelpayouts returned malformed flight data", retryable=True)

        pending: list[tuple[dict[str, Any], str]] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            if (
                item.get("origin") != request.origin
                or item.get("destination") != request.destination
            ):
                continue
            departure_at = item.get("departure_at")
            return_at = item.get("return_at")
            if (
                not isinstance(departure_at, str)
                or departure_at[:10] != request.depart_date.isoformat()
            ):
                continue
            if request.return_date is not None and (
                not isinstance(return_at, str)
                or return_at[:10] != request.return_date.isoformat()
            ):
                continue
            if request.direct_only and item.get("transfers") != 0:
                continue
            price = item.get("price")
            direct_url = self._direct_aviasales_url(item.get("link"))
            if not isinstance(price, int | float) or direct_url is None:
                continue
            offer = {
                "origin": item.get("origin"),
                "destination": item.get("destination"),
                "origin_airport": item.get("origin_airport"),
                "destination_airport": item.get("destination_airport"),
                "departure_at": departure_at,
                "return_at": return_at,
                "price": price,
                "currency": request.currency,
                "trip_class": request.trip_class,
                "airline": item.get("airline"),
                "flight_number": item.get("flight_number"),
                "transfers": item.get("transfers"),
                "return_transfers": item.get("return_transfers"),
                "duration_minutes": item.get("duration"),
            }
            pending.append((offer, direct_url))
        pending.sort(key=lambda pair: pair[0]["price"])
        pending = pending[: request.limit]
        partner_links = await self.create_partner_links([url for _, url in pending])
        offers: list[dict[str, Any]] = []
        for offer, direct_url in pending:
            offer["partner_url"] = partner_links[direct_url]
            offer["affiliate_verified"] = True
            offer["affiliate_provider"] = "travelpayouts"
            offers.append(offer)
        response = {
            "source": "travelpayouts_aviasales_data_cache",
            "live_availability": False,
            "notice": "Cached prices are indicative and must be rechecked on Aviasales.",
            "query": request.model_dump(mode="json"),
            "count": len(offers),
            "offers": offers,
        }
        if not offers:
            response["empty_reason"] = "no_matching_cached_observations"
            response["empty_message"] = (
                "No exact cached observation was found; this does not mean flights are unavailable."
            )
        return response

    async def analyze_calendar(self, request: CalendarAnalysisRequest) -> dict[str, Any]:
        # Публичный контракт MCP сохраняет понятные названия старого календарного API,
        # а актуальный endpoint Travelpayouts ожидает поля с суффиксом ``_at``.  # noqa: RUF003
        provider_group_by = {
            "departure_date": "departure_at",
            "return_date": "return_at",
        }[request.calendar_type]
        params: dict[str, Any] = {
            "origin": request.origin,
            "destination": request.destination,
            "departure_at": request.month,
            "group_by": provider_group_by,
            "currency": request.currency.lower(),
            "market": "ru",
            "direct": str(request.direct_only).lower(),
        }
        if request.return_month:
            params["return_at"] = request.return_month
        if request.trip_length is not None:
            params["min_trip_duration"] = request.trip_length
            params["max_trip_duration"] = request.trip_length
        payload = await self._get("/aviasales/v3/grouped_prices", params)
        raw = payload.get("data", {})
        if not isinstance(raw, dict):
            raise UpstreamError("Travelpayouts returned malformed calendar data", retryable=True)

        entries: list[dict[str, Any]] = []
        for day, item in raw.items():
            if not isinstance(day, str) or not isinstance(item, dict):
                continue
            transfers = item.get("transfers")
            if request.direct_only and transfers != 0:
                continue
            price = item.get("price")
            direct_url = self._direct_aviasales_url(item.get("link"))
            if not isinstance(price, int | float) or direct_url is None:
                continue
            entries.append(
                {
                    "date": day,
                    "price": price,
                    "transfers": transfers,
                    "return_transfers": item.get("return_transfers"),
                    "airline": item.get("airline"),
                    "flight_number": item.get("flight_number"),
                    "departure_at": item.get("departure_at"),
                    "return_at": item.get("return_at"),
                    "_direct_url": direct_url,
                }
            )
        analysis = analyze_calendar_entries(entries, request.currency)
        best = analysis["best_dates"]
        partner_links = await self.create_partner_links(
            [entry["_direct_url"] for entry in best]
        )
        for entry in analysis["daily_prices"]:
            direct_url = entry.pop("_direct_url")
            if direct_url in partner_links:
                entry["partner_url"] = partner_links[direct_url]
                entry["affiliate_verified"] = True
                entry["affiliate_provider"] = "travelpayouts"
        response = {
            "source": "travelpayouts_aviasales_data_cache",
            "live_availability": False,
            "notice": "Prices are cached; partner links for cheapest dates are verified.",
            "query": request.model_dump(mode="json"),
            **analysis,
        }
        if not analysis["daily_prices"]:
            response["empty_reason"] = "no_cached_calendar_observations"
            response["empty_message"] = (
                "No cached calendar observation was found; try live search for an exact itinerary."
            )
        return response

    @staticmethod
    def _signature_values(value: Any) -> list[str]:
        """Разворачивает вложенный JSON в отсортированный список значений для MD5-подписи."""
        if isinstance(value, dict):
            values: list[str] = []
            for key in sorted(value):
                if key != "signature":
                    values.extend(TravelpayoutsClient._signature_values(value[key]))
            return values
        if isinstance(value, list):
            values = []
            for item in value:
                values.extend(TravelpayoutsClient._signature_values(item))
            return values
        if isinstance(value, bool):
            return [str(value).lower()]
        return [str(value)]

    def _live_signature(self, body: dict[str, Any]) -> str:
        """Создаёт обязательную подпись новой версии Flight Search API."""
        source = ":".join(
            [self.settings.travelpayouts_token, *self._signature_values(body)]
        )
        # MD5 здесь является форматом подписи внешнего API.
        # Он не используется для криптографической защиты.
        return hashlib.md5(source.encode(), usedforsecurity=False).hexdigest()

    async def _live_post(
        self,
        url: str,
        body: dict[str, Any],
        headers: dict[str, str],
        *,
        allow_not_modified: bool = False,
    ) -> dict[str, Any] | None:
        """Выполняет POST к live API и безопасно классифицирует его ошибки."""  # noqa: RUF002
        try:
            response = await self.client.post(url, json=body, headers=headers)
            if allow_not_modified and response.status_code == 304:
                return None
            response.raise_for_status()
            payload = response.json()
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise UpstreamError(
                "Travelpayouts live search is temporarily unavailable", retryable=True
            ) from exc
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if status == 403:
                message = "Travelpayouts live search access is not enabled for this project"
            elif status == 429:
                message = "Travelpayouts live search rate limit reached"
            else:
                message = f"Travelpayouts live search returned HTTP {status}"
            raise UpstreamError(message, retryable=status == 429 or status >= 500) from exc
        except ValueError as exc:
            raise UpstreamError(
                "Travelpayouts live search returned invalid JSON", retryable=True
            ) from exc
        if not isinstance(payload, dict):
            raise UpstreamError(
                "Travelpayouts live search returned an invalid response", retryable=True
            )
        return payload

    def _results_endpoint(self, results_url: Any) -> str:
        """Строит endpoint результатов и блокирует подмену домена из upstream-ответа."""
        if not isinstance(results_url, str):
            raise UpstreamError("Travelpayouts live search omitted results URL", retryable=True)
        normalized_url = (
            results_url if "://" in results_url else f"https://{results_url.lstrip('/')}"
        )
        parsed = urlsplit(normalized_url)
        hostname = (parsed.hostname or "").lower()
        if parsed.scheme != "https" or not (
            hostname == "travelpayouts.com" or hostname.endswith(".travelpayouts.com")
        ):
            raise UpstreamError(
                "Travelpayouts live search returned an unsafe results URL",
                retryable=False,
            )
        return f"https://{parsed.netloc}/search/affiliate/results"

    @staticmethod
    def _price_value(price: Any) -> tuple[float | None, str | None]:
        """Извлекает сумму и валюту из допустимых вариантов ответа провайдера."""
        if isinstance(price, int | float):
            return float(price), None
        if not isinstance(price, dict):
            return None, None
        value = price.get("amount", price.get("value"))
        currency = price.get("currency", price.get("currency_code"))
        return (
            float(value) if isinstance(value, int | float) else None,
            str(currency).upper() if isinstance(currency, str) else None,
        )

    @staticmethod
    def _unix_datetime(value: Any) -> str | None:
        """Преобразует Unix timestamp провайдера в UTC ISO 8601."""
        if not isinstance(value, int | float):
            return None
        return datetime.fromtimestamp(value, tz=UTC).isoformat()

    def _normalize_live_batch(
        self,
        payload: dict[str, Any],
        request: LiveFlightSearchRequest,
    ) -> list[dict[str, Any]]:
        """Преобразует пакет live-результатов в компактные безопасные предложения."""
        raw_legs = payload.get("flight_legs", [])
        raw_tickets = payload.get("tickets", [])
        raw_agents = payload.get("agents", [])
        legs = raw_legs if isinstance(raw_legs, list) else []
        tickets = raw_tickets if isinstance(raw_tickets, list) else []
        agents = raw_agents if isinstance(raw_agents, list) else []
        agent_names = {
            item.get("id"): item.get("label") or item.get("gate_name")
            for item in agents
            if isinstance(item, dict)
        }
        normalized: list[dict[str, Any]] = []
        for ticket in tickets:
            if not isinstance(ticket, dict):
                continue
            segments: list[dict[str, Any]] = []
            transfer_count = 0
            for segment in ticket.get("segments", []):
                if not isinstance(segment, dict):
                    continue
                segment_legs: list[dict[str, Any]] = []
                for reference in segment.get("flights", []):
                    leg = (
                        legs[reference]
                        if isinstance(reference, int) and 0 <= reference < len(legs)
                        else None
                    )
                    if not isinstance(leg, dict):
                        continue
                    segment_legs.append(
                        {
                            "origin": leg.get("origin"),
                            "destination": leg.get("destination"),
                            "departure_at": leg.get("local_departure_date_time")
                            or self._unix_datetime(leg.get("departure_unix_timestamp")),
                            "arrival_at": leg.get("local_arrival_date_time")
                            or self._unix_datetime(leg.get("arrival_unix_timestamp")),
                            "flight": leg.get("operating_carrier_designator"),
                        }
                    )
                transfer_count += max(0, len(segment_legs) - 1)
                segments.append({"flights": segment_legs})
            if request.direct_only and transfer_count > 0:
                continue
            for proposal in ticket.get("proposals", []):
                if not isinstance(proposal, dict):
                    continue
                amount, currency = self._price_value(proposal.get("price"))
                if amount is None:
                    continue
                normalized.append(
                    {
                        "price": amount,
                        "currency": currency or request.currency,
                        "agency": agent_names.get(proposal.get("agent_id")),
                        "transfers": transfer_count,
                        "segments": segments,
                        "booking_link_available_on_user_click": True,
                    }
                )
        return normalized

    async def live_search_flights(self, request: LiveFlightSearchRequest) -> dict[str, Any]:
        """Запускает реальный поиск и собирает обновления до завершения или дедлайна."""
        if not self.settings.affiliate_configured:
            raise UpstreamError(
                "Travelpayouts live search credentials are not configured", retryable=False
            )
        class_codes = {"economy": "Y", "business": "C", "first": "F", "comfort": "W"}
        directions = [
            {
                "origin": request.origin,
                "destination": request.destination,
                "date": request.depart_date.isoformat(),
            }
        ]
        if request.return_date is not None:
            directions.append(
                {
                    "origin": request.destination,
                    "destination": request.origin,
                    "date": request.return_date.isoformat(),
                }
            )
        body: dict[str, Any] = {
            "currency_code": request.currency,
            "locale": self.settings.live_search_locale,
            "marker": str(self.settings.travelpayouts_marker),
            "market_code": self.settings.live_search_market,
            "search_params": {
                "directions": directions,
                "passengers": {
                    "adults": request.adults,
                    "children": request.children,
                    "infants": request.infants,
                },
                "trip_class": class_codes[request.trip_class],
            },
        }
        signature = self._live_signature(body)
        body["signature"] = signature
        headers = {
            "x-affiliate-user-id": self.settings.travelpayouts_token,
            "x-signature": signature,
            "x-user-ip": request.user_ip,
            "User-Agent": request.user_agent,
            "Referer": request.referer,
        }
        started = await self._live_post(
            f"{self.settings.live_search_base_url}/search/affiliate/start",
            body,
            headers,
        )
        if started is None:
            raise UpstreamError("Travelpayouts live search did not start", retryable=True)
        search_id = started.get("search_id")
        if not isinstance(search_id, str) or not search_id:
            raise UpstreamError("Travelpayouts live search omitted search ID", retryable=True)
        results_endpoint = self._results_endpoint(started.get("results_url"))
        last_update: int | float = 0
        offers: list[dict[str, Any]] = []
        seen: set[str] = set()
        complete = False
        deadline = asyncio.get_running_loop().time() + self.settings.live_search_timeout_seconds - 2
        while asyncio.get_running_loop().time() < deadline:
            payload = await self._live_post(
                results_endpoint,
                {"search_id": search_id, "last_update_timestamp": last_update},
                headers,
                allow_not_modified=True,
            )
            if payload is not None:
                for offer in self._normalize_live_batch(payload, request):
                    fingerprint = repr(offer)
                    if fingerprint not in seen:
                        seen.add(fingerprint)
                        offers.append(offer)
                updated = payload.get("last_update_timestamp")
                if isinstance(updated, int | float):
                    last_update = updated
                complete = payload.get("is_over") is True
                if complete:
                    break
            await asyncio.sleep(self.settings.live_search_poll_interval_seconds)
        offers.sort(key=lambda item: item["price"])
        offers = offers[: request.limit]
        response = {
            "source": "travelpayouts_aviasales_live_search",
            "live_availability": True,
            "complete": complete,
            "query": request.model_dump(
                mode="json", exclude={"user_ip", "user_agent", "referer"}
            ),
            "count": len(offers),
            "offers": offers,
            "notice": (
                "Live prices; booking links must only be requested after an explicit user click."
            ),
        }
        if not offers:
            response["empty_reason"] = (
                "no_live_offers" if complete else "live_search_collection_incomplete"
            )
            response["empty_message"] = (
                "The live search completed without offers."
                if complete
                else "The live search deadline expired before any offers arrived."
            )
        return response


async def execute_with_retries(
    operation: Callable[[], Awaitable[dict[str, Any]]],
    *,
    max_attempts: int,
    base_delay: float = 0.1,
) -> dict[str, Any]:
    for attempt in range(1, max_attempts + 1):
        try:
            return await operation()
        except UpstreamError as exc:
            if not exc.retryable or attempt == max_attempts:
                raise
            delay = base_delay * (2 ** (attempt - 1))
            if exc.retry_after_seconds is not None:
                delay = max(delay, min(exc.retry_after_seconds, 5.0))
            await asyncio.sleep(delay)
    raise AssertionError("unreachable")
