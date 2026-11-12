import json

import pytest
from mcp import Client
from mcp.server.mcpserver.exceptions import ToolError

from agentic_ems import mcp_server
from agentic_ems.semantic import catalogue_text, load_catalog, meter_tree_text

PERIOD = {"start": "2026-10-19", "end": "2026-10-21"}
DAILY_BANDS = {"metric": "cost_eur", "node": "site", "grain": "day", "by_tariff_band": True} | PERIOD
ANCESTOR_LIST = {"metric": "consumption_kwh", "meters": ["dept_a", "line_a2"]} | PERIOD


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(scope="module")
def server(con):
    return mcp_server.build_server(con)


def text(result) -> str:
    return result.content[0].text


@pytest.mark.anyio
async def test_list_tools_return_the_catalogue_and_the_tree(server, con):
    catalog = load_catalog(con)
    assert text(await server.call_tool("list_metrics", {})) == catalogue_text(catalog)
    assert text(await server.call_tool("list_meters", {})) == meter_tree_text(catalog)


@pytest.mark.anyio
async def test_query_metric_returns_columns_rows_and_sql(server):
    result = await server.call_tool("query_metric", {"request": DAILY_BANDS})
    out = result.structured_content
    assert out["columns"] == ["bucket_start", "tariff_band", "value"]
    assert out["rows"][0][:2] == ["2026-10-19T00:00:00+02:00", "F1"]  # bucket_start stays ISO text with its offset
    assert out["sql"].startswith("SELECT ")


@pytest.mark.anyio
async def test_ancestor_list_is_an_error_result(server):
    with pytest.raises(ToolError, match="ancestor"):
        await server.call_tool("query_metric", {"request": ANCESTOR_LIST})


@pytest.mark.anyio
async def test_client_round_trip(server):
    async with Client(server, raise_exceptions=True) as client:
        assert {tool.name for tool in (await client.list_tools()).tools} == {"list_metrics", "list_meters", "query_metric"}
        ok = await client.call_tool("query_metric", {"request": DAILY_BANDS})
        assert not ok.is_error
        assert json.loads(text(ok)) == ok.structured_content
        assert len(ok.structured_content["rows"]) == 6  # two days, three tariff bands each
        refused = await client.call_tool("query_metric", {"request": ANCESTOR_LIST})
        assert refused.is_error
        assert "ancestor" in text(refused)
        assert refused.structured_content is None


def test_missing_database_exits_with_the_command(tmp_path, monkeypatch):
    monkeypatch.setattr(mcp_server, "DEFAULT_DB", tmp_path / "plant.duckdb")
    with pytest.raises(SystemExit, match="agentic_ems.generator"):
        mcp_server.main()
