import duckdb
import pytest
import sqlglot

from agentic_ems.benchmark import sql_arm
from agentic_ems.benchmark.sql_arm import run_sql
from agentic_ems.semantic import LimitError, RequestError, execute_limited


@pytest.fixture
def paths(tmp_path):
    other_db, some_csv = tmp_path / "other.duckdb", tmp_path / "some.csv"
    duckdb.connect(str(other_db)).close()
    some_csv.write_text("a,b\n1,2\n")
    return {"other_db": other_db, "out_csv": tmp_path / "out.csv", "some_csv": some_csv}


def test_select_works(con):
    result = run_sql(con, "SELECT meter_id, level FROM meter WHERE parent_id = 'site' ORDER BY meter_id")
    assert result.columns == ["meter_id", "level"]
    assert result.rows == [(f"dept_{d}", "department") for d in "abcd"]
    assert result.sql.startswith("SELECT meter_id") and result.params == []


@pytest.mark.parametrize(
    "sql",
    [
        "WITH d AS (SELECT meter_id FROM meter WHERE level = 'department') SELECT count(*) FROM d",
        "SELECT count(*) FROM meter WHERE level = 'department' UNION ALL SELECT count(*) FROM meter WHERE level = 'line'",
        "SELECT count(*) FROM meter WHERE level = 'department'; -- a trailing comment",
        "FROM meter SELECT count(*) WHERE level = 'department'",
    ],
)
def test_read_queries_are_accepted(con, sql):
    assert run_sql(con, sql).rows[0] == (4,)


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO meter VALUES ('x','x','site',NULL,'consumption')",
        "SET lock_configuration = false",
        "SET enable_external_access = true",
        "ATTACH '{other_db}' AS o",
        "COPY meter TO '{out_csv}'",
        "SELECT * FROM read_csv('{some_csv}')",
        "SELECT 1; SELECT 2",
        "INSTALL httpfs",
        "CREATE TABLE t AS SELECT 1",
    ],
)
def test_statement_is_refused(con, paths, sql):
    with pytest.raises(RequestError):
        run_sql(con, sql.format(**paths))
    assert con.execute("SELECT count(*) FROM meter").fetchall()[0][0] > 0
    assert not paths["out_csv"].exists()


# DuckDB would run most of these on the locked database, so only the parser stops them.
@pytest.mark.parametrize(
    "sql",
    [
        "PRAGMA table_info('meter')",
        "CALL pragma_version()",
        "EXPLAIN SELECT 1",
        "DESCRIBE meter",
        "SHOW TABLES",
        "VALUES (1)",
        "EXPORT DATABASE '{out_csv}'",
        "WITH d AS (DELETE FROM meter RETURNING *) SELECT * FROM d",
        "WITH d AS (DESCRIBE meter) SELECT * FROM d",
        "SELECT * FROM (DESCRIBE meter)",
        "SELECT * FROM (SHOW TABLES)",
        "SELECT * FROM meter, LATERAL (SHOW TABLES)",
        "",
    ],
)
def test_parser_refuses_anything_but_one_select(con, paths, sql):
    with pytest.raises(RequestError, match="only one SELECT query|could not be parsed"):
        run_sql(con, sql.format(**paths))


# These would run, and show settings or file paths of the host in a published transcript.
@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM duckdb_settings()",
        "SELECT * FROM pragma_version()",
        "SELECT current_setting('secret_directory')",
        "SELECT * FROM pg_catalog.pg_settings",
        "SELECT path FROM duckdb_databases",
        "SELECT * FROM query('SELECT * FROM duckdb_settings()')",
    ],
)
def test_host_details_are_refused(con, sql):
    with pytest.raises(RequestError, match="is not available"):
        run_sql(con, sql)


def test_information_schema_is_available(con):
    result = run_sql(con, "SELECT count(*) FROM information_schema.columns WHERE table_name = 'reading'")
    assert result.rows == [(9,)]


def test_original_text_is_executed(con, monkeypatch):
    sql = "FROM meter SELECT count(*)"
    assert sqlglot.transpile(sql, read="duckdb", write="duckdb") != [sql]
    executed = []

    def spy(con, text, params=None):
        executed.append(text)
        return execute_limited(con, text, params)

    monkeypatch.setattr(sql_arm, "execute_limited", spy)
    assert run_sql(con, sql).sql == sql
    assert executed == [sql]


def test_time_zone_values_can_be_returned(con):
    assert len(run_sql(con, "SELECT now() AS t").rows) == 1
    [(value,)] = run_sql(con, "SELECT TIMESTAMPTZ '2026-07-01 00:00:00+00' AS t").rows
    assert value.isoformat() == "2026-07-01T02:00:00+02:00"


def test_engine_error_keeps_its_message(con):
    with pytest.raises(RequestError, match="no_such_table"):
        run_sql(con, "SELECT * FROM no_such_table")


@pytest.mark.parametrize(
    "sql",
    [
        "ATTACH '{other_db}' AS o",
        "COPY meter TO '{out_csv}'",
        "SELECT * FROM read_csv('{some_csv}')",
        "SET lock_configuration = false",
        "INSTALL httpfs",
    ],
)
def test_database_barrier_without_the_parser(con, paths, sql):
    with pytest.raises(duckdb.Error):
        execute_limited(con, sql.format(**paths))
    assert not paths["out_csv"].exists()
    assert con.execute("SELECT count(*) FROM meter").fetchall()[0][0] > 0


def test_row_limit_applies(con):
    assert len(run_sql(con, "SELECT * FROM range(5000)").rows) == 5000
    with pytest.raises(LimitError, match="5,000"):
        run_sql(con, "SELECT * FROM reading")
