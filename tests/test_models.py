from __future__ import annotations

import pytest
from pydantic import ValidationError

from aviasales_mcp.models import (
    CalendarAnalysisRequest,
    FlightSearchRequest,
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
