from __future__ import annotations

import pytest
from mcp import Client

from aviasales_mcp.server import mcp


@pytest.mark.asyncio
async def test_server_exposes_exactly_two_tools() -> None:
    async with Client(mcp) as client:
        result = await client.list_tools()
    assert {tool.name for tool in result.tools} == {"search_flights", "analyze_price_calendar"}
    assert len(result.tools) == 2


@pytest.mark.asyncio
async def test_invalid_arguments_return_safe_tool_error() -> None:
    async with Client(mcp) as client:
        result = await client.call_tool(
            "search_flights",
            {"origin": "12!", "destination": "LED", "depart_date": "not-a-date"},
        )
    assert result.is_error is True
    assert result.content[0].text.endswith("Invalid flight search parameters")
    assert "12!" not in result.content[0].text
