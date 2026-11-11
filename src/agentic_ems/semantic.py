"""The semantic layer: the four metrics, the hierarchy rules, and the request they accept.

Every request is checked here before any SQL is built. The central rule: a meter is never
added to one of its own ancestors, because the ancestor's reading already includes it.
"""

from dataclasses import dataclass
from datetime import date
from typing import Literal, get_args

from pydantic import BaseModel, ConfigDict

Level = Literal["site", "department", "line", "machine"]
Grain = Literal["15min", "hour", "day", "month", "total"]
MetricName = Literal["consumption_kwh", "peak_kw", "cost_eur", "energy_per_unit"]
LEVELS: tuple[str, ...] = get_args(Level)
GRAINS: tuple[str, ...] = get_args(Grain)


@dataclass(frozen=True)
class Metric:
    unit: str
    description: str
    allows_tariff_split: bool


METRICS: dict[str, Metric] = {
    "consumption_kwh": Metric("kWh", "Energy drawn by the scope's meters, summed over the period.", True),
    "peak_kw": Metric(
        "kW",
        "Highest coincident demand: per 15-minute interval, the scope's kWh summed and times 4, "
        "then the maximum over the period.",
        False,
    ),
    "cost_eur": Metric("EUR", "Energy cost: each interval's kWh times the tariff price of its band, summed.", True),
    "energy_per_unit": Metric(
        "kWh/unit",
        "A line's own kWh divided by the units produced on it, as a ratio of totals per result cell.",
        False,
    ),
}

RULES: dict[str, str] = {
    "R1": "A parent meter's reading includes all of its children, plus unmetered load and losses.",
    "R2": "A node's total is the node's own meter, never a sum of a lower level; "
    "a list of meters may never contain a meter together with one of its ancestors, or a meter twice.",
    "R3": "peak_kw is coincident: per interval, sum the kWh of the scope's meters and multiply by 4, "
    "then take the maximum.",
    "R4": "cost_eur prices each interval's kWh at the tariff of that interval's band.",
    "R5": "energy_per_unit is total kWh divided by total units, never an average of ratios; "
    "an order's units are spread evenly over its duration, so a period or grain boundary takes its share.",
    "R6": "energy_per_unit is null where no units were produced.",
    "R7": "The four metrics read consumption meters only; solar (generation) and battery (storage) are rejected.",
    "R8": "Reading timestamps are the UTC start of each 15-minute interval.",
    "R9": "Days, months and tariff bands use the plant's local-time columns.",
    "R10": "A period includes its start date and excludes its end date, in plant local dates.",
}


class RequestError(ValueError):
    """A request the semantic layer refuses; the message says why."""


class MetricRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    metric: MetricName
    node: str | None = None
    breakdown: Level | None = None
    meters: list[str] | None = None
    start: date
    end: date
    grain: Grain = "total"
    by_tariff_band: bool = False


@dataclass(frozen=True)
class Meter:
    id: str
    name: str
    level: str | None
    parent_id: str | None
    kind: str


@dataclass(frozen=True)
class Catalog:
    meters: dict[str, Meter]
    first_date: date
    end_date: date


def load_catalog(con) -> Catalog:
    rows = con.execute("SELECT meter_id, name, level, parent_id, kind FROM meter ORDER BY meter_id").fetchall()
    first_date, end_date = con.execute("SELECT first_date, end_date FROM dataset_info").fetchall()[0]
    return Catalog({row[0]: Meter(*row) for row in rows}, first_date, end_date)


def _ancestors(meter_id: str, catalog: Catalog) -> list[str]:
    found = []
    parent = catalog.meters[meter_id].parent_id
    while parent is not None:
        found.append(parent)
        parent = catalog.meters[parent].parent_id
    return found


def validate(request: MetricRequest, catalog: Catalog) -> None:
    """Raise RequestError unless the request is one the compiler may turn into SQL."""
    if request.node is not None and request.meters is not None:
        raise RequestError("give either node or meters, not both")
    if request.meters is not None and request.breakdown is not None:
        raise RequestError("a breakdown applies to a node, not to a list of meters")
    if request.meters == []:
        raise RequestError("meters must name at least one meter")
    scope = request.meters if request.meters is not None else [request.node or "site"]

    for meter_id in scope:
        if meter_id not in catalog.meters:
            raise RequestError(f"unknown meter {meter_id!r}")
        if catalog.meters[meter_id].kind != "consumption":
            raise RequestError(
                f"{meter_id!r} is a {catalog.meters[meter_id].kind} meter; the metrics cover consumption meters only"
            )
    for i, meter_id in enumerate(scope):
        if meter_id in scope[:i]:
            raise RequestError(f"meter {meter_id!r} appears twice in meters")
        for ancestor in _ancestors(meter_id, catalog):
            if ancestor in scope:
                raise RequestError(
                    f"meters {ancestor!r} and {meter_id!r} cannot be added: {ancestor!r} is an ancestor of "
                    f"{meter_id!r} and its reading already includes it"
                )

    level = catalog.meters[scope[0]].level
    if request.breakdown is not None and LEVELS.index(request.breakdown) <= LEVELS.index(str(level)):
        raise RequestError(
            f"breakdown {request.breakdown!r} is not below node {scope[0]!r}, which is at level {level!r}"
        )

    if request.metric == "energy_per_unit" and not (
        (request.meters is None and level == "line" and request.breakdown is None)
        or (request.meters is None and level in ("site", "department") and request.breakdown == "line")
    ):
        raise RequestError(
            "energy_per_unit needs a single line as node, or the site or a department with breakdown 'line'"
        )
    if request.by_tariff_band and not METRICS[request.metric].allows_tariff_split:
        raise RequestError(f"{request.metric} cannot be split by tariff band; only consumption_kwh and cost_eur can")

    if request.start >= request.end:
        raise RequestError(f"start {request.start} must be before end {request.end}")
    if request.start < catalog.first_date or request.end > catalog.end_date:
        raise RequestError(
            f"period {request.start} to {request.end} is outside the dataset: "
            f"data from {catalog.first_date.isoformat()} up to but excluding {catalog.end_date.isoformat()}"
        )


def catalogue_text(catalog: Catalog) -> str:
    lines = ["Metrics:"]
    for name, metric in METRICS.items():
        split = "can" if metric.allows_tariff_split else "cannot"
        lines.append(f"- {name} ({metric.unit}): {metric.description} It {split} be split by tariff band.")
    lines.append(f"Levels: {', '.join(LEVELS)}. Grains: {', '.join(GRAINS)}.")
    lines.append(f"Data from {catalog.first_date.isoformat()} up to but excluding {catalog.end_date.isoformat()}.")
    lines.append("Rules:")
    lines += [f"- {rule_id}: {text}" for rule_id, text in RULES.items()]
    return "\n".join(lines)


def meter_tree_text(catalog: Catalog) -> str:
    children: dict[str | None, list[Meter]] = {}
    for meter in catalog.meters.values():
        children.setdefault(meter.parent_id, []).append(meter)
    lines: list[str] = []

    def walk(meter: Meter, depth: int) -> None:
        lines.append(f"{'  ' * depth}{meter.id} ({meter.name}, {meter.level or meter.kind})")
        for child in children.get(meter.id, []):
            walk(child, depth + 1)

    for root in sorted(children.get(None, []), key=lambda meter: meter.kind != "consumption"):
        walk(root, 0)
    return "\n".join(lines)


def tool_schema() -> dict:
    return MetricRequest.model_json_schema()
