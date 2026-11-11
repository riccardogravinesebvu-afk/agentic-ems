"""The text-to-SQL arm's one tool: run a single read query written by the model.

Two barriers: the parser refuses anything that is not one SELECT, and the database instance
behind `open_db` is read-only with file and network access switched off and its settings locked.
"""

import re

import duckdb
import sqlglot
from sqlglot import exp

from agentic_ems.semantic import QueryResult, RequestError, execute_limited

# Statement nodes that sqlglot can place inside a query, in a CTE or a parenthesised subquery.
NOT_READ = (
    exp.DML, exp.DDL, exp.Command, exp.Pragma, exp.Describe, exp.Show, exp.Summarize,
    exp.Set, exp.Attach, exp.Detach, exp.Install, exp.Use, exp.Drop,
)
ONLY_SELECT = "only one SELECT query can run (a SELECT, a UNION of SELECTs, or WITH ... SELECT)"


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
        or tree.find(*NOT_READ) is not None
    ):
        raise RequestError(ONLY_SELECT)
    try:
        columns, rows = execute_limited(con, sql)  # the original text, never sqlglot's re-rendering
    except duckdb.Error as exc:
        raise RequestError(str(exc)) from None
    return QueryResult(columns, rows, sql, [])
