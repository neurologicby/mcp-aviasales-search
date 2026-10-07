from __future__ import annotations

from datetime import date, timedelta

import pytest
from pydantic import ValidationError

from aviasales_mcp.models import (
    CalendarAnalysisRequest,
    FlightSearchRequest,
    LiveFlightSearchRequest,
    analyze_calendar_entries,
)


def test_search_normalizes_iata_and_validates_dates() -> None:
    request = FlightSearchRequest(
        origin=" mow ", destination="led", depart_date="2026-11-10", return_date="2026-11-15"
    )
    assert request.origin == "MOW"
    assert request.destination == "LED"

    with pytest.raises(ValidationError):
        FlightSearchRequest(
            origin="MOW",
            destination="MOW",
            depart_date="2026-11-10",
        )
    with pytest.raises(ValidationError):
        FlightSearchRequest(
            origin="MOW",
            destination="LED",
            depart_date="2026-11-10",
            return_date="2026-11-09",
        )


def test_calendar_validation() -> None:
    with pytest.raises(ValidationError):
        CalendarAnalysisRequest(origin="MOW", destination="LED", month="2026-13")
    with pytest.raises(ValidationError):
        CalendarAnalysisRequest(origin="M0W", destination="LED", month="2026-11")


def test_live_search_validates_user_context_and_passengers() -> None:
    request = LiveFlightSearchRequest(
        origin=" mow ",
        destination="led",
        depart_date=date.today() + timedelta(days=10),
        adults=2,
        infants=1,
        user_ip="203.0.113.10",
        user_agent="Browser/1.0",
        referer="https://example.com/flights",
    )
    assert request.origin == "MOW"
    assert request.user_ip == "203.0.113.10"

    with pytest.raises(ValidationError):
        LiveFlightSearchRequest(
            origin="MOW",
            destination="LED",
            depart_date=date.today() + timedelta(days=10),
            adults=1,
            infants=1,
            user_ip="not-an-ip",
            user_agent="Browser/1.0",
            referer="https://example.com/flights",
        )


def test_calendar_analysis_statistics_and_trend() -> None:
    result = analyze_calendar_entries(
        [
            {"date": "2026-11-01", "price": 100},
            {"date": "2026-11-02", "price": 110},
            {"date": "2026-11-20", "price": 150},
            {"date": "2026-11-21", "price": 160},
        ],
        "USD",
    )
    assert result["summary"] == {
        "minimum": 100.0,
        "maximum": 160.0,
        "average": 130.0,
        "median": 130.0,
        "trend": "rising",
        "change_between_month_halves_percent": 47.62,
    }
    assert result["best_dates"][0]["date"] == "2026-11-01"


def test_empty_calendar_analysis() -> None:
    result = analyze_calendar_entries([], "RUB")
    assert result["days_with_prices"] == 0
    assert result["summary"] is None
