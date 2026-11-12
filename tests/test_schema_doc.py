import inspect
from pathlib import Path

from agentic_ems.benchmark.sql_arm import HOST_NAMES, HOST_PREFIXES
from agentic_ems.generator import PRICES, generate
from agentic_ems.semantic import (
    ROW_LIMIT,
    RULES,
    TIME_LIMIT_MS,
    load_catalog,
    meter_tree_text,
)

SCHEMA_DOC = (Path(__file__).resolve().parents[1] / "docs" / "schema.md").read_text()


def test_every_rule_id_is_in_schema_doc():
    for rule_id in RULES:
        assert f"**{rule_id}.**" in SCHEMA_DOC


def test_every_column_is_in_schema_doc(con):
    columns = con.execute("SELECT table_name, column_name FROM information_schema.columns").fetchall()
    assert len(columns) == 26
    for table, column in columns:
        assert f"{table}.{column}" in SCHEMA_DOC


def test_schema_doc_states_the_facts_the_code_holds(con):
    # The text-to-SQL arm learns these only from the doc, so the doc must not drift from the code.
    assert f"```\n{meter_tree_text(load_catalog(con))}\n```" in SCHEMA_DOC
    assert ", ".join(f"{band} {price:.2f}" for band, price in PRICES.items()) in SCHEMA_DOC
    assert f"at most {ROW_LIMIT:,} rows" in SCHEMA_DOC
    assert f"at most {TIME_LIMIT_MS / 1000:g} seconds" in SCHEMA_DOC
    for name in [*HOST_PREFIXES, *HOST_NAMES]:
        assert f"`{name}`" in SCHEMA_DOC
    period = inspect.signature(generate).parameters
    assert f"starts on {period['start'].default} and ends before {period['end'].default}" in SCHEMA_DOC
