"""The text-to-SQL arm's one tool: run a single read query written by the model.

Two barriers: the parser refuses anything that is not one SELECT, and the database instance
behind `open_db` is read-only with file and network access switched off and its settings locked.
"""

import re

import duckdb
import sqlglot
from sqlglot import exp

from agentic_ems.agent import MAX_TOOL_CALLS, Arm, make_arm
from agentic_ems.semantic import (
    Catalog,
    QueryResult,
    RequestError,
    execute_limited,
    meter_tree_text,
)

# Backstop: statement nodes that sqlglot can place inside a query.
NOT_READ = (
    exp.DML, exp.DDL, exp.Command, exp.Pragma, exp.Describe, exp.Show, exp.Summarize,
    exp.Set, exp.Attach, exp.Detach, exp.Install, exp.Use, exp.Drop,
)
ONLY_SELECT = "only one SELECT query can run (a SELECT, a UNION of SELECTs, or WITH ... SELECT)"
# Functions and catalog views that reveal host details (settings, file paths) or run SQL from a string.
HOST_PREFIXES = ("duckdb_", "pragma_", "pg_")
HOST_NAMES = {"current_setting", "which_secret", "query", "query_table", "json_execute_serialized_sql"}


def _host_name(tree: exp.Expr) -> str | None:
    for func in tree.find_all(exp.Func):
        name = func.name if isinstance(func, exp.Anonymous) else func.sql_name()
        if name.lower().startswith(HOST_PREFIXES) or name.lower() in HOST_NAMES:
            return name
    # Catalog views such as pg_catalog.pg_settings or duckdb_databases are table references.
    return next((t.name for t in tree.find_all(exp.Table) if t.name.lower().startswith(HOST_PREFIXES)), None)


def run_sql(con, sql: str) -> QueryResult:
    try:
        statements = sqlglot.parse(sql, read="duckdb")
    except sqlglot.errors.SqlglotError as exc:
        # sqlglot underlines the bad token with terminal escape codes; the model reads plain text.
        raise RequestError("the SQL could not be parsed: " + re.sub(r"\x1b\[\d+m", "", str(exc))) from None
    # A trailing comment after the last semicolon parses as an empty Semicolon statement.
    statements = [s for s in statements if s is not None and not isinstance(s, exp.Semicolon)]
    if len(statements) != 1:
        raise RequestError(f"{ONLY_SELECT}; this text has {len(statements)} statements")
    tree = statements[0]
    if (
        not isinstance(tree, exp.Query)
        or any(not isinstance(cte.this, exp.Query) for cte in tree.find_all(exp.CTE))
        or any(not isinstance(sub.this, (exp.Query, exp.Pivot)) for sub in tree.find_all(exp.Subquery))
        or tree.find(*NOT_READ) is not None
    ):
        raise RequestError(ONLY_SELECT)
    if (name := _host_name(tree)) is not None:
        raise RequestError(f"{name} is not available: it reads the engine's settings or catalog, not the plant data")
    try:
        columns, rows = execute_limited(con, sql)  # the original text, never sqlglot's re-rendering
    except duckdb.Error as exc:
        raise RequestError(str(exc)) from None
    return QueryResult(columns, rows, sql, [])


def _run_tool(con, args: dict) -> QueryResult:
    if not isinstance(args.get("sql"), str):
        raise RequestError("sql: a string with one SELECT query is required")
    return run_sql(con, args["sql"])


SCHEMA_ONLY = "sql_schema_only"


def schema_only_md(con, catalog: Catalog, schema_md: str) -> str:
    """The context of the arm that gets no rules: how the tool runs, then tables, columns and types read from the
    database and the meter tree. Nothing else of docs/schema.md: no column meanings, no metric definitions, no rules."""
    tool = schema_md[schema_md.index("## How queries run"):schema_md.index("## Questions with no answer")]
    lines = ["# Plant database schema", "", "The database is DuckDB.", "", tool.rstrip(), "", "## Tables and columns"]
    columns = con.execute(
        "SELECT table_name, column_name, data_type FROM information_schema.columns "
        "WHERE table_schema = 'main' ORDER BY table_name, ordinal_position"
    ).fetchall()
    for table in dict.fromkeys(row[0] for row in columns):
        lines += ["", f"### {table}", ""] + [f"- `{table}.{name}` {kind}" for t, name, kind in columns if t == table]
    lines += ["", "## The meter tree", "", "Each line shows the meter id, its name and its level (or its kind).", "",
              "```", meter_tree_text(catalog), "```"]
    return "\n".join(lines) + "\n"


def sql_arm(con, schema_md: str, max_tool_calls: int = MAX_TOOL_CALLS, name: str = "sql") -> Arm:
    tool = {
        "name": "run_sql",
        "description": "Run one read-only SQL query against the plant database and return its rows.",
        "input_schema": {"type": "object", "properties": {"sql": {"type": "string"}}, "required": ["sql"]},
    }
    return make_arm(name, tool, schema_md, con, _run_tool, max_tool_calls=max_tool_calls)
