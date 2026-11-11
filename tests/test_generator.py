import hashlib
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import duckdb

from agentic_ems.generator import generate

ROME = ZoneInfo("Europe/Rome")
TABLES = ["meter", "reading", "tariff", "production_order", "dataset_info"]


def local(ts_utc):
    return ts_utc.replace(tzinfo=UTC).astimezone(ROME)


def fingerprint(path):
    con = duckdb.connect(str(path), read_only=True)
    out = {"sum_kwh": con.execute("SELECT sum(kwh) FROM reading").fetchall()[0]}
    for table in TABLES:
        rows = con.execute(f"SELECT * FROM {table} ORDER BY ALL").fetchall()
        out[table] = (len(rows), hashlib.md5(repr(rows).encode()).hexdigest())
    con.close()
    return out


def test_same_seed_same_database(tmp_path):
    span = {"start": date(2026, 5, 4), "end": date(2026, 5, 7)}
    generate(tmp_path / "a.duckdb", **span)
    generate(tmp_path / "b.duckdb", **span)
    first = fingerprint(tmp_path / "a.duckdb")
    assert first["reading"][0] == 43 * 3 * 96
    assert first == fingerprint(tmp_path / "b.duckdb")


def test_parent_margin_holds_every_interval(con):
    rows = con.execute("""
        WITH children AS (
            SELECT m.parent_id, r.ts_utc, sum(r.kwh) AS total
            FROM reading r JOIN meter m USING (meter_id)
            WHERE m.parent_id IS NOT NULL
            GROUP BY m.parent_id, r.ts_utc
        )
        SELECT c.parent_id, min(r.kwh / c.total), max(r.kwh / c.total)
        FROM children c JOIN reading r ON r.meter_id = c.parent_id AND r.ts_utc = c.ts_utc
        GROUP BY c.parent_id
    """).fetchall()
    assert len(rows) == 1 + 4 + 10
    for _, low, high in rows:
        assert 1.03 - 1e-3 <= low and high <= 1.08 + 1e-3
        assert high - low < 2e-3


def test_autumn_day_has_100_intervals(con):
    count, hours, days = con.execute("""
        SELECT count(*), count(DISTINCT hour_start), count(DISTINCT day_start)
        FROM reading WHERE meter_id = 'site' AND local_date = DATE '2026-10-25'
    """).fetchall()[0]
    assert (count, hours, days) == (100, 25, 1)
    months = con.execute("""
        SELECT count(DISTINCT month_start) FROM reading
        WHERE local_date BETWEEN DATE '2026-10-01' AND DATE '2026-10-31'
    """).fetchall()[0]
    assert months == (1,)


def test_spring_day_has_92_intervals(tmp_path):
    path = tmp_path / "spring.duckdb"
    generate(path, start=date(2026, 3, 23), end=date(2026, 4, 5))
    con = duckdb.connect(str(path), read_only=True)
    count = con.execute("""
        SELECT count(*) FROM reading
        WHERE meter_id = 'site' AND local_date = DATE '2026-03-29'
    """).fetchall()[0]
    con.close()
    assert count == (92,)


def test_orders_never_overlap_on_a_line(con):
    assert con.execute("SELECT count(*) FROM production_order").fetchall()[0][0] > 0
    overlaps = con.execute("""
        SELECT count(*) FROM production_order a JOIN production_order b
        ON a.line_id = b.line_id AND a.order_id < b.order_id
        AND a.start_utc < b.end_utc AND b.start_utc < a.end_utc
    """).fetchall()[0]
    assert overlaps == (0,)


def test_orders_on_quarter_hours(con):
    for start, end in con.execute("SELECT start_utc, end_utc FROM production_order").fetchall():
        for ts in (start, end):
            assert ts.minute in (0, 15, 30, 45) and ts.second == 0 and ts.microsecond == 0
        assert start < end


def test_solar_nonnegative_battery_signed(con):
    assert con.execute("SELECT min(kwh) FROM reading WHERE meter_id = 'solar'").fetchall()[0][0] >= 0
    low, high = con.execute("SELECT min(kwh), max(kwh) FROM reading WHERE meter_id = 'battery'").fetchall()[0]
    assert low < 0 < high


def test_band_matches_definition(con):
    samples = {
        "2026-10-25T12:00:00+01:00": "F3",  # Sunday noon
        "2026-10-20T10:00:00+02:00": "F1",  # Tuesday
        "2026-10-24T10:00:00+02:00": "F2",  # Saturday
    }
    for local_start, band in samples.items():
        rows = con.execute(
            "SELECT DISTINCT tariff_band FROM reading WHERE local_start = ?", [local_start]
        ).fetchall()
        assert rows == [(band,)]


def test_every_meter_has_every_interval(con):
    counts = con.execute("SELECT count(*) FROM reading GROUP BY meter_id").fetchall()
    assert len(counts) == 43
    assert set(counts) == {(13 * 96 + 4,)}
    assert con.execute("SELECT count(*) FROM reading WHERE kwh IS NULL").fetchall()[0] == (0,)


def test_no_orders_on_sunday(con):
    for start, end in con.execute("SELECT start_utc, end_utc FROM production_order").fetchall():
        assert local(start).weekday() != 6
        assert local(end - timedelta(seconds=1)).weekday() != 6


def test_two_lines_peak_at_different_instants(con):
    def peak(line):
        return con.execute(
            "SELECT arg_max(ts_utc, kwh) FROM reading WHERE meter_id = ?", [line]
        ).fetchall()[0]

    assert peak("line_a1") != peak("line_b1")


def test_load_follows_production(con):
    inside, outside = con.execute("""
        SELECT
            avg(kwh) FILTER (WHERE active),
            avg(kwh) FILTER (WHERE NOT active)
        FROM (
            SELECT r.kwh, EXISTS (
                SELECT 1 FROM production_order o
                WHERE o.line_id = 'line_a1' AND o.start_utc <= r.ts_utc AND r.ts_utc < o.end_utc
            ) AS active
            FROM reading r WHERE r.meter_id = 'line_a1'
        )
    """).fetchall()[0]
    assert inside > 2 * outside


def site_mean(con, day, first_hour, last_hour):
    return con.execute(
        """
        SELECT avg(kwh) FROM reading
        WHERE meter_id = 'site' AND local_date = ?
        AND substr(local_start, 12, 2)::INTEGER >= ? AND substr(local_start, 12, 2)::INTEGER < ?
        """,
        [day, first_hour, last_hour],
    ).fetchall()[0][0]


def test_weekday_day_above_night_above_nothing(con):
    day = site_mean(con, date(2026, 10, 27), 10, 12)
    night = site_mean(con, date(2026, 10, 27), 2, 4)
    assert day > night >= 41 * 0.5 * 0.9


def test_sunday_is_standby(con):
    sunday = site_mean(con, date(2026, 10, 25), 0, 24)
    tuesday = site_mean(con, date(2026, 10, 27), 0, 24)
    assert sunday < 0.6 * tuesday


def test_closure_has_no_orders(tmp_path):
    path = tmp_path / "closure.duckdb"
    generate(path, start=date(2025, 12, 20), end=date(2026, 1, 10))
    con = duckdb.connect(str(path), read_only=True)
    orders = con.execute("SELECT start_utc, end_utc FROM production_order").fetchall()
    con.close()
    closure_start = datetime(2025, 12, 24, tzinfo=ROME)
    closure_end = datetime(2026, 1, 7, tzinfo=ROME)
    assert orders
    for start, end in orders:
        assert local(end) <= closure_start or local(start) >= closure_end


def test_solar_zero_at_night_and_seasonal(tmp_path):
    totals = {}
    for day in (date(2026, 6, 16), date(2025, 12, 16)):
        path = tmp_path / f"{day}.duckdb"
        generate(path, start=day, end=day + timedelta(days=1))
        con = duckdb.connect(str(path), read_only=True)
        rows = con.execute("SELECT local_start, kwh FROM reading WHERE meter_id = 'solar'").fetchall()
        con.close()
        for local_start, kwh in rows:
            hour = int(local_start[11:13])
            if hour >= 23 or hour < 4:
                assert kwh == 0
        totals[day] = sum(kwh for _, kwh in rows)
    assert totals[date(2026, 6, 16)] > totals[date(2025, 12, 16)] > 0
