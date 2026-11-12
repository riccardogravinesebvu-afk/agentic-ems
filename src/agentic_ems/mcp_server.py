"""The semantic layer over MCP: three tools on stdio, behind the same `query` the agent calls."""

import sys
from typing import Any

import duckdb
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from agentic_ems.semantic import (
    DEFAULT_DB,
    LimitError,
    MetricRequest,
    RequestError,
    catalogue_text,
    load_catalog,
    meter_tree_text,
    open_db,
    query,
)


def build_server(con: duckdb.DuckDBPyConnection) -> MCPServer:
    catalog = load_catalog(con)
    mcp = MCPServer("agentic-ems")

    @mcp.tool()
    def list_metrics() -> str:
        """The metrics, levels, grains, data period and the rules every request follows."""
        return catalogue_text(catalog)

    @mcp.tool()
    def list_meters() -> str:
        """The meter tree: each meter's id, name and level, children indented under their parent."""
        return meter_tree_text(catalog)

    @mcp.tool()
    def query_metric(request: MetricRequest) -> dict[str, Any]:
        """Run one metric request against the plant data and return its columns, rows and SQL."""
        try:
            # A cursor per call: the server runs sync tools on worker threads.
            with con.cursor() as cursor:
                result = query(request, cursor, catalog)
        except (RequestError, LimitError) as exc:
            raise ToolError(str(exc)) from exc
        return {"columns": result.columns, "rows": result.rows, "sql": result.sql}

    return mcp


def main() -> None:
    try:
        con = open_db(DEFAULT_DB)
    except FileNotFoundError as exc:
        sys.exit(str(exc))  # the message goes to stderr; stdout carries only the protocol
    build_server(con).run()
