from pathlib import Path

from agentic_ems.semantic import RULES

SCHEMA_DOC = (Path(__file__).resolve().parents[1] / "docs" / "schema.md").read_text()


def test_every_rule_id_is_in_schema_doc():
    for rule_id in RULES:
        assert f"**{rule_id}.**" in SCHEMA_DOC


def test_every_column_is_in_schema_doc(con):
    columns = con.execute("SELECT table_name, column_name FROM information_schema.columns").fetchall()
    assert len(columns) == 26
    for table, column in columns:
        assert f"{table}.{column}" in SCHEMA_DOC
