from datetime import date

import duckdb
import pytest

from agentic_ems import semantic
from agentic_ems.semantic import (
    LimitError,
    RequestError,
    execute_limited,
    load_catalog,
    open_db,
    query,
)

CROSS_JOIN = "SELECT sum(a.range * b.range) FROM range(100000000) a, range(100000000) b"
PERIOD = {"start": "2026-10-19", "end": "2026-11-01"}


@pytest.fixture(scope="module")
def catalog(con):
    return load_catalog(con)


def test_query_returns_rows_and_sql(con, catalog):
    result = query({"metric": "consumption_kwh", "breakdown": "department"} | PERIOD, con, catalog)
    assert result.columns == ["meter_id", "value"]
    assert len(result.rows) == 4
    assert result.sql.startswith("SELECT ")
    assert result.params == [["dept_a", "dept_b", "dept_c", "dept_d"], date(2026, 10, 19), date(2026, 11, 1)]


def test_dict_request_is_validated(con, catalog):
    with pytest.raises(RequestError, match="colour.*not permitted"):
        query({"metric": "consumption_kwh", "colour": "red"} | PERIOD, con, catalog)
    with pytest.raises(RequestError, match="start"):
        query({"metric": "consumption_kwh", "start": "yesterday", "end": "2026-11-01"}, con, catalog)


def test_missing_database_names_the_command(tmp_path):
    with pytest.raises(FileNotFoundError, match="uv run python -m agentic_ems.generator"):
        open_db(tmp_path / "missing.duckdb")


def test_row_limit(con, catalog):
    with pytest.raises(LimitError, match="5,000"):
        query({"metric": "consumption_kwh", "breakdown": "machine", "grain": "15min"} | PERIOD, con, catalog)
    columns, rows = execute_limited(con, "SELECT * FROM range(5000)")
    assert len(rows) == 5000 and columns == ["range"]
    with pytest.raises(LimitError, match="5,000"):
        execute_limited(con, "SELECT * FROM range(5001)")


def test_time_limit_is_set(con):
    assert con.execute("SELECT current_setting('max_execution_time')").fetchall() == [(10000,)]
    # SET GLOBAL: a cursor or a second connection to the instance gets the same limit.
    assert con.cursor().execute("SELECT current_setting('max_execution_time')").fetchall() == [(10000,)]


def test_timeout_is_a_limit_error(tmp_path, monkeypatch):
    path = tmp_path / "slow.duckdb"
    duckdb.connect(str(path)).close()
    monkeypatch.setattr(semantic, "TIME_LIMIT_MS", 200)
    with pytest.raises(LimitError, match=r"time limit of 0\.2 seconds"):
        execute_limited(open_db(path), CROSS_JOIN)


def test_timeout_message_names_the_real_limit():
    # The message comes from TIME_LIMIT_MS; a short engine timeout avoids waiting the real 10 seconds.
    con = duckdb.connect()
    con.execute("SET max_execution_time = 200")
    with pytest.raises(LimitError, match="time limit of 10 seconds"):
        execute_limited(con, CROSS_JOIN)


def test_lockdown_covers_cursor_and_second_connection(db_path, con):
    settings = (
        "SELECT current_setting('enable_external_access'), current_setting('autoinstall_known_extensions'), "
        "current_setting('autoload_known_extensions'), current_setting('max_execution_time'), "
        "current_setting('lock_configuration')"
    )
    second = duckdb.connect(str(db_path), read_only=True)
    try:
        for connection in (con.cursor(), second):
            assert connection.execute(settings).fetchall() == [(False, False, False, 10000, True)]
    finally:
        second.close()
    with pytest.raises(duckdb.Error):
        con.cursor().execute("SET SESSION max_execution_time = 0")


def test_open_db_is_cached(db_path, con):
    assert open_db(db_path) is open_db(str(db_path)) is con


def test_database_is_read_only(con):
    with pytest.raises(duckdb.Error):
        con.execute("CREATE TABLE scratch (a INTEGER)")
    with pytest.raises(duckdb.Error):
        con.execute("SET lock_configuration = false")


def test_database_readable_after_lockdown(con):
    assert con.execute("SELECT count(*) FROM meter").fetchall()[0][0] > 0
    assert con.execute("SELECT current_setting('enable_external_access')").fetchall() == [(False,)]
