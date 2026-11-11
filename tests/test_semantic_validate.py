import re
from datetime import date

import pytest

from agentic_ems.semantic import (
    GRAINS,
    LEVELS,
    METRICS,
    RULES,
    Catalog,
    Meter,
    MetricRequest,
    RequestError,
    catalogue_text,
    load_catalog,
    meter_tree_text,
    tool_schema,
    validate,
)

PERIOD = {"start": date(2026, 10, 19), "end": date(2026, 11, 1)}


@pytest.fixture(scope="module")
def catalog(con):
    return load_catalog(con)


def request(**fields):
    return MetricRequest(**({"metric": "consumption_kwh"} | PERIOD | fields))


def rejects(catalog, *fragments, **fields):
    with pytest.raises(RequestError) as error:
        validate(request(**fields), catalog)
    for fragment in fragments:
        assert fragment in str(error.value)


def test_list_with_ancestor_is_rejected(catalog):
    rejects(catalog, "dept_a", "line_a2", meters=["dept_a", "line_a2"])
    rejects(catalog, "site", "mach_b1_2", meters=["mach_b1_2", "line_c1", "site"])


def test_list_with_repeat_is_rejected(catalog):
    rejects(catalog, "line_a1", meters=["line_a1", "line_a1"])


def test_generation_meter_is_rejected(catalog):
    rejects(catalog, "consumption meters only", node="solar")
    rejects(catalog, "consumption meters only", meters=["site", "solar"])
    rejects(catalog, "consumption meters only", node="battery")


def test_breakdown_not_below_node(catalog):
    rejects(catalog, "line_a1", "department", node="line_a1", breakdown="department")
    rejects(catalog, "not below", node="line_a1", breakdown="line")
    rejects(catalog, "not below", breakdown="site")
    levelless = Catalog({"x": Meter("x", "X", None, None, "consumption")}, PERIOD["start"], PERIOD["end"])
    with pytest.raises(RequestError, match="no level"):
        validate(request(node="x", breakdown="line"), levelless)


def test_unknown_meter(catalog):
    rejects(catalog, "unknown meter", "dept_z", node="dept_z")
    rejects(catalog, "unknown meter", "dept_z", meters=["dept_a", "dept_z"])
    rejects(catalog, "unknown meter", node="")


def test_node_and_meters_together(catalog):
    rejects(catalog, "node", "meters", node="dept_a", meters=["line_a1"])


def test_period_outside_dataset(catalog):
    message = "data from 2026-10-19 up to but excluding 2026-11-01"
    rejects(catalog, message, start=date(2026, 10, 18), end=date(2026, 10, 20))
    rejects(catalog, message, start=date(2026, 10, 30), end=date(2026, 11, 2))
    rejects(catalog, message, start=date(2026, 12, 1), end=date(2026, 12, 5))
    rejects(catalog, message, start=date(2026, 10, 1), end=date(2026, 10, 5))


def test_start_not_before_end(catalog):
    rejects(catalog, "before", start=date(2026, 10, 20), end=date(2026, 10, 20))
    rejects(catalog, "before", start=date(2026, 10, 22), end=date(2026, 10, 20))


def test_tariff_split_only_consumption_and_cost(catalog):
    for metric in ("peak_kw", "energy_per_unit"):
        rejects(catalog, "tariff band", metric=metric, node="line_a1", by_tariff_band=True)
    for metric in ("consumption_kwh", "cost_eur"):
        validate(request(metric=metric, by_tariff_band=True), catalog)


def test_energy_per_unit_scope(catalog):
    for scope in ({"node": "line_b2"}, {"breakdown": "line"}, {"node": "dept_c", "breakdown": "line"}):
        validate(request(metric="energy_per_unit", **scope), catalog)
    for scope in (
        {"node": "mach_a1_1"},
        {"meters": ["line_a1", "line_a2"]},
        {},
        {"node": "dept_a"},
        {"node": "dept_a", "breakdown": "machine"},
        {"node": "line_a1", "breakdown": "machine"},
    ):
        rejects(catalog, "energy_per_unit", metric="energy_per_unit", **scope)


def test_hostile_strings(catalog):
    rejects(catalog, "unknown meter", node="site'; DROP TABLE meter;--")
    rejects(catalog, "unknown meter", meters=["line_a1", "x' OR '1'='1"])


def test_valid_requests_pass(catalog):
    for fields in (
        {"metric": "consumption_kwh", "node": "dept_a", "breakdown": "line", "grain": "day", "by_tariff_band": True},
        {"metric": "peak_kw", "meters": ["line_a1", "line_b2"], "grain": "hour"},
        {"metric": "cost_eur", "by_tariff_band": True, "start": date(2026, 10, 25), "end": date(2026, 10, 26)},
        {"metric": "energy_per_unit", "node": "line_a1", "grain": "15min"},
    ):
        validate(request(**fields), catalog)
    assert {metric for metric in METRICS} == {"consumption_kwh", "peak_kw", "cost_eur", "energy_per_unit"}


def enum(prop):
    return next(option["enum"] for option in prop.get("anyOf", [prop]) if "enum" in option)


def test_tool_schema_lists_closed_sets():
    props = tool_schema()["properties"]
    assert enum(props["metric"]) == list(METRICS)
    assert enum(props["breakdown"]) == list(LEVELS)
    assert enum(props["grain"]) == list(GRAINS)
    assert tool_schema()["additionalProperties"] is False


def test_texts_are_rendered_from_the_layer(catalog):
    text = catalogue_text(catalog)
    for name in [*METRICS, *RULES]:
        assert name in text
    tree = meter_tree_text(catalog)
    assert re.search(r"^  dept_a .*\n    line_a1 .*\n      mach_a1_1 ", tree, re.MULTILINE)
    assert re.search(r"^solar ", tree, re.MULTILINE)
