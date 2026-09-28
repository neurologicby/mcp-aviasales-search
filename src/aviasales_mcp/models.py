from __future__ import annotations

from datetime import UTC, date, datetime
from statistics import fmean, median
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

Currency = Literal["RUB", "USD", "EUR"]
TripClass = Literal["economy"]
CalendarType = Literal["departure_date", "return_date"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FlightSearchRequest(StrictModel):
    origin: str = Field(min_length=2, max_length=3)
    destination: str = Field(min_length=2, max_length=3)
    depart_date: date
    return_date: date | None = None
    currency: Currency = "RUB"
    trip_class: TripClass = "economy"
    direct_only: bool = False
    limit: int = Field(default=10, ge=1, le=10)

    @field_validator("origin", "destination", mode="before")
    @classmethod
    def normalize_iata(cls, value: str) -> str:
        normalized = value.strip().upper()
        if not normalized.isalpha():
            raise ValueError("must contain only Latin letters")
        return normalized

    @model_validator(mode="after")
    def validate_route_and_dates(self) -> FlightSearchRequest:
        if self.origin == self.destination:
            raise ValueError("origin and destination must differ")
        if self.return_date is not None and self.return_date < self.depart_date:
            raise ValueError("return_date cannot be before depart_date")
        return self


class CalendarAnalysisRequest(StrictModel):
    origin: str = Field(min_length=3, max_length=3)
    destination: str = Field(min_length=3, max_length=3)
    month: str = Field(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")
    return_month: str | None = Field(default=None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$")
    calendar_type: CalendarType = "departure_date"
    trip_length: int | None = Field(default=None, ge=1, le=365)
    currency: Currency = "RUB"
    direct_only: bool = False

    @field_validator("origin", "destination", mode="before")
    @classmethod
    def normalize_iata(cls, value: str) -> str:
        normalized = value.strip().upper()
        if not normalized.isalpha():
            raise ValueError("must contain only Latin letters")
        return normalized

    @model_validator(mode="after")
    def validate_route(self) -> CalendarAnalysisRequest:
        if self.origin == self.destination:
            raise ValueError("origin and destination must differ")
        return self


def parse_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def analyze_calendar_entries(entries: list[dict[str, Any]], currency: str) -> dict[str, Any]:
    priced = [entry for entry in entries if isinstance(entry.get("price"), int | float)]
    priced.sort(key=lambda item: (item["price"], item.get("date", "")))
    if not priced:
        return {
            "currency": currency,
            "days_with_prices": 0,
            "summary": None,
            "best_dates": [],
            "daily_prices": [],
        }

    prices = [float(item["price"]) for item in priced]
    chronological = sorted(priced, key=lambda item: item.get("date", ""))
    midpoint = max(1, len(chronological) // 2)
    first_mean = fmean(float(item["price"]) for item in chronological[:midpoint])
    second_half = chronological[midpoint:]
    second_mean = (
        fmean(float(item["price"]) for item in second_half) if second_half else first_mean
    )
    change = 0.0 if first_mean == 0 else ((second_mean - first_mean) / first_mean) * 100
    trend = "stable" if abs(change) < 5 else ("rising" if change > 0 else "falling")
    return {
        "currency": currency,
        "days_with_prices": len(priced),
        "summary": {
            "minimum": min(prices),
            "maximum": max(prices),
            "average": round(fmean(prices), 2),
            "median": round(float(median(prices)), 2),
            "trend": trend,
            "change_between_month_halves_percent": round(change, 2),
        },
        "best_dates": priced[: min(5, len(priced))],
        "daily_prices": chronological,
    }
