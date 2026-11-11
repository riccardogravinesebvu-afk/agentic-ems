from collections import defaultdict
from datetime import date, datetime, timedelta

import pytest

from agentic_ems.semantic import Catalog, Meter, MetricRequest, compile, load_catalog

PERIOD = {"start": date(2026, 10, 19), "end": date(2026, 11, 1)}


@pytest.fixture(scope="module")
def catalog(con):
    return load_catalog(con)


def request(**fields):
    return MetricRequest(**({"metric": "consumption_kwh"} | PERIOD | fields))


def run(con, catalog, **fields):
    compiled = compile(request(**fields), catalog)
    return compiled, con.execute(compiled.sql, compiled.params).fetchall()


def raw(con, meter_ids, start, end):
    """(meter_id, ts_utc, kwh, tariff_band) rows of the given meters in the half-open local-date period."""
    return con.execute(
        "SELECT meter_id, ts_utc, kwh, tariff_band FROM reading WHERE meter_id IN ? AND local_date >= ? AND local_date < ?",
        [meter_ids, start, end],
    ).fetchall()


def kwh_sum(con, meter_ids, start=PERIOD["start"], end=PERIOD["end"]):
    return sum(row[2] for row in raw(con, meter_ids, start, end))


def test_site_total_is_site_meter_only(con, catalog):
    compiled, rows = run(con, catalog)
    assert compiled.columns == ["value"]
    [(value,)] = rows
    assert value == pytest.approx(kwh_sum(con, ["site"]))
    departments = [m.id for m in catalog.meters.values() if m.level == "department"]
    assert abs(kwh_sum(con, ["site", *departments]) - value) / value > 0.03
    assert value / kwh_sum(con, departments) > 1.03


def test_breakdown_has_one_row_per_descendant(con, catalog):
    compiled, rows = run(con, catalog, breakdown="line")
    assert compiled.columns == ["meter_id", "value"]
    lines = sorted(m.id for m in catalog.meters.values() if m.level == "line")
    assert len(lines) == 10
    assert [row[0] for row in rows] == lines
    for meter_id, value in rows:
        assert value == pytest.approx(kwh_sum(con, [meter_id]))


def test_list_of_two_lines_is_their_sum(con, catalog):
    compiled, rows = run(con, catalog, meters=["line_a1", "line_b2"])
    assert compiled.columns == ["value"]
    [(value,)] = rows
    assert value == pytest.approx(kwh_sum(con, ["line_a1"]) + kwh_sum(con, ["line_b2"]))


def test_hourly_on_autumn_day_has_25_buckets(con, catalog):
    compiled, rows = run(con, catalog, start=date(2026, 10, 25), end=date(2026, 10, 26), grain="hour")
    assert compiled.columns == ["bucket_start", "value"]
    buckets = [row[0] for row in rows]
    assert len(set(buckets)) == len(buckets) == 25
    assert [b for b in buckets if b.startswith("2026-10-25T02:00:00")] == [
        "2026-10-25T02:00:00+02:00",
        "2026-10-25T02:00:00+01:00",
    ]
    instants = [datetime.fromisoformat(b) for b in buckets]
    assert instants == sorted(instants)
    hourly = defaultdict(float)
    for _, ts, kwh, _ in raw(con, ["site"], date(2026, 10, 25), date(2026, 10, 26)):
        hourly[ts.replace(minute=0)] += kwh
    assert [row[1] for row in rows] == pytest.approx([hourly[ts] for ts in sorted(hourly)])


def test_period_end_is_excluded(con, catalog):
    _, [(value,)] = run(con, catalog, start=date(2026, 10, 20), end=date(2026, 10, 21))
    assert value == pytest.approx(kwh_sum(con, ["site"], date(2026, 10, 20), date(2026, 10, 21)))
    assert value < kwh_sum(con, ["site"], date(2026, 10, 20), date(2026, 10, 22))
    _, rows = run(con, catalog, start=date(2026, 10, 20), end=date(2026, 10, 22), grain="day")
    assert [row[0] for row in rows] == ["2026-10-20T00:00:00+02:00", "2026-10-21T00:00:00+02:00"]


def test_peak_is_coincident(con, catalog):
    pair = ["line_a1", "line_b1"]
    _, [(value,)] = run(con, catalog, metric="peak_kw", meters=pair)
    per_ts: dict[datetime, float] = defaultdict(float)
    separate: dict[str, float] = defaultdict(float)
    for meter_id, ts, kwh, _ in raw(con, pair, PERIOD["start"], PERIOD["end"]):
        per_ts[ts] += kwh
        separate[meter_id] = max(separate[meter_id], kwh * 4)
    assert value == pytest.approx(max(per_ts.values()) * 4)
    assert value <= sum(separate.values())


def test_cost_uses_interval_tariff(con, catalog):
    _, [(value,)] = run(con, catalog, metric="cost_eur")
    prices = dict(con.execute("SELECT tariff_band, price_eur_per_kwh FROM tariff").fetchall())
    rows = raw(con, ["site"], PERIOD["start"], PERIOD["end"])
    assert value == pytest.approx(sum(kwh * prices[band] for _, _, kwh, band in rows))


def test_tariff_split_rows(con, catalog):
    _, [(total,)] = run(con, catalog)
    compiled, rows = run(con, catalog, by_tariff_band=True)
    assert compiled.columns == ["tariff_band", "value"]
    assert [row[0] for row in rows] == ["F1", "F2", "F3"]
    assert sum(row[1] for row in rows) == pytest.approx(total)
    _, daily = run(con, catalog, start=date(2026, 10, 24), end=date(2026, 10, 27), grain="day", by_tariff_band=True)
    assert [row[:2] for row in daily] == [
        ("2026-10-24T00:00:00+02:00", "F2"),
        ("2026-10-24T00:00:00+02:00", "F3"),
        ("2026-10-25T00:00:00+02:00", "F3"),
        ("2026-10-26T00:00:00+01:00", "F1"),
        ("2026-10-26T00:00:00+01:00", "F2"),
        ("2026-10-26T00:00:00+01:00", "F3"),
    ]


def prorated(con, line_id, start, end):
    """Per local date: (line kWh, units), each order's units spread evenly over its 15-minute intervals."""
    dates = dict(con.execute("SELECT ts_utc, local_date FROM reading WHERE meter_id = ?", [line_id]).fetchall())
    cells: dict[date, list[float]] = defaultdict(lambda: [0.0, 0.0])
    for _, ts, kwh, _ in raw(con, [line_id], start, end):
        cells[dates[ts]][0] += kwh
    orders = con.execute("SELECT start_utc, end_utc, units FROM production_order WHERE line_id = ?", [line_id])
    for order_start, order_end, units in orders.fetchall():
        intervals = (order_end - order_start) // timedelta(minutes=15)
        for i in range(intervals):
            day = dates[order_start + i * timedelta(minutes=15)]
            if start <= day < end:
                cells[day][1] += units / intervals
    return cells


def test_energy_per_unit_is_ratio_of_totals(con, catalog):
    # line_a3 runs a night shift, so orders cross both period boundaries and are cut.
    start, end = date(2026, 10, 20), date(2026, 10, 23)
    _, [(value,)] = run(con, catalog, metric="energy_per_unit", node="line_a3", start=start, end=end)
    cells = prorated(con, "line_a3", start, end)
    kwh, units = sum(c[0] for c in cells.values()), sum(c[1] for c in cells.values())
    assert value == pytest.approx(kwh / units)
    assert value != pytest.approx(sum(c[0] / c[1] for c in cells.values()) / len(cells))


def test_energy_per_unit_null_without_units(con, catalog):
    start, end = date(2026, 10, 19), date(2026, 10, 26)
    compiled, rows = run(con, catalog, metric="energy_per_unit", node="line_a1", start=start, end=end, grain="day")
    assert compiled.columns == ["bucket_start", "value"]
    assert len(rows) == 7
    by_day = dict(rows)
    assert by_day["2026-10-25T00:00:00+02:00"] is None
    cells = prorated(con, "line_a1", start, end)
    expected = [c[0] / c[1] for day, c in sorted(cells.items()) if day != date(2026, 10, 25)]
    assert [v for b, v in rows if not b.startswith("2026-10-25")] == pytest.approx(expected)


def test_compile_is_pure():
    meters = [("site", "site", None), ("dept_a", "department", "site"), ("line_a1", "line", "dept_a")]
    catalog = Catalog(
        {i: Meter(i, i, level, parent, "consumption") for i, level, parent in meters}, PERIOD["start"], PERIOD["end"]
    )
    req = request(metric="energy_per_unit", breakdown="line", grain="day")
    first, second = compile(req, catalog), compile(req, catalog)
    assert (first.sql, first.params, first.columns) == (second.sql, second.params, second.columns)
    assert first.params == [["line_a1"], PERIOD["start"], PERIOD["end"]]


def test_no_literal_values_in_sql(catalog):
    for fields in (
        {"node": "dept_b", "breakdown": "machine", "grain": "15min"},
        {"meters": ["line_a1", "line_c2"], "metric": "peak_kw", "grain": "month"},
        {"node": "line_d1", "metric": "energy_per_unit"},
        {"metric": "cost_eur", "by_tariff_band": True, "grain": "day"},
    ):
        compiled = compile(request(**fields), catalog)
        assert "'" not in compiled.sql
        assert "2026" not in compiled.sql
        for meter_id in catalog.meters:
            assert meter_id not in compiled.sql
        assert compiled.params[1:] == [PERIOD["start"], PERIOD["end"]]
