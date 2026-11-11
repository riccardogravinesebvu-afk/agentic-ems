"""The semantic layer: the four metrics, the hierarchy rules, and the request they accept.

Every request is checked here before any SQL is built. The central rule: a meter is never
added to one of its own ancestors, because the ancestor's reading already includes it.
"""

import functools
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Literal, get_args

import duckdb
from pydantic import BaseModel, ConfigDict, ValidationError

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
    "a list of meters may never contain a meter twice, or a meter together with one of its ancestors, "
    "because the ancestor's reading already contains the descendant's energy, "
    "so the two together equal the ancestor's own reading.",
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
    scope = request.meters if request.meters is not None else [request.node if request.node is not None else "site"]

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
    if level is None:
        raise RequestError(f"meter {scope[0]!r} has no level in the meter table, so it cannot be a node")
    if request.breakdown is not None and LEVELS.index(request.breakdown) <= LEVELS.index(level):
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


BUCKETS = {"15min": "local_start", "hour": "hour_start", "day": "day_start", "month": "month_start"}
# Per metric: the value expression and the FROM clause it reads; peak_kw is built in compile.
VALUES = {
    "consumption_kwh": ("sum(r.kwh)", "reading r"),
    "cost_eur": ("sum(r.kwh * t.price_eur_per_kwh)", "reading r JOIN tariff t ON t.tariff_band = r.tariff_band"),
    "energy_per_unit": (
        "sum(r.kwh) / NULLIF(sum(o.units / (epoch(o.end_utc - o.start_utc) / 900)), 0)",
        (
            "reading r LEFT JOIN production_order o ON o.line_id = r.meter_id "
            "AND r.ts_utc >= o.start_utc AND r.ts_utc < o.end_utc"
        ),
    ),
}


@dataclass
class Compiled:
    sql: str
    params: list
    columns: list[str]


def compile(request: MetricRequest, catalog: Catalog) -> Compiled:
    """Turn a valid request into SQL with bound parameters; identifiers come only from closed sets."""
    validate(request, catalog)
    if request.meters is not None:
        scope = list(request.meters)
    else:
        node = request.node if request.node is not None else "site"
        scope = (
            [node]
            if request.breakdown is None
            else [m.id for m in catalog.meters.values() if m.level == request.breakdown and node in _ancestors(m.id, catalog)]
        )

    keys = []  # (expression, column name)
    if request.breakdown is not None:
        keys.append(("r.meter_id", "meter_id"))
    if request.grain != "total":
        keys.append((f"r.{BUCKETS[request.grain]}", "bucket_start"))
    if request.by_tariff_band:
        keys.append(("r.tariff_band", "tariff_band"))
    exprs = [expr for expr, _ in keys]

    where = "WHERE r.meter_id IN ? AND r.local_date >= ? AND r.local_date < ?"
    if request.metric == "peak_kw":
        inner = ", ".join([*exprs, "r.ts_utc"])
        value, source = "max(r.kw)", f"(SELECT {inner}, sum(r.kwh) * 4 AS kw FROM reading r {where} GROUP BY {inner}) r"
        where = ""
    else:
        value, source = VALUES[request.metric]

    select = ", ".join([f"{expr} AS {name}" for expr, name in keys] + [f"{value} AS value"])
    sql = f"SELECT {select} FROM {source} {where}".rstrip()
    if keys:
        # Bucket strings do not sort in time order across a clock change, so order by the bucket's first instant.
        order = [
            f"min(min(r.ts_utc)) OVER (PARTITION BY {expr})" if name == "bucket_start" else expr for expr, name in keys
        ]
        sql += f" GROUP BY {', '.join(exprs)} ORDER BY {', '.join(order)}"
    return Compiled(sql, [scope, request.start, request.end], [name for _, name in keys] + ["value"])


DEFAULT_DB = Path(__file__).resolve().parents[2] / "data" / "plant.duckdb"
ROW_LIMIT = 5000
TIME_LIMIT_MS = 10000


@dataclass
class QueryResult:
    columns: list[str]
    rows: list[tuple]
    sql: str
    params: list


class LimitError(RuntimeError):
    """A query went past the row limit or the time limit."""


def open_db(path: Path | str = DEFAULT_DB) -> duckdb.DuckDBPyConnection:
    """The one read-only, locked-down connection per database file in this process."""
    return _open_db(Path(path).resolve())


@functools.cache
def _open_db(path: Path) -> duckdb.DuckDBPyConnection:
    if not path.exists():
        raise FileNotFoundError("data/plant.duckdb not found. Run: uv run python -m agentic_ems.generator")
    con = duckdb.connect(str(path), read_only=True)
    # GLOBAL: these settings cover the whole instance, so every cursor and connection on it; the lock goes last.
    for setting in (
        "enable_external_access = false",
        "autoinstall_known_extensions = false",
        "autoload_known_extensions = false",
        f"max_execution_time = {TIME_LIMIT_MS}",
        "TimeZone = 'Europe/Rome'",  # time zone values in results do not depend on the host
        "lock_configuration = true",
    ):
        con.execute(f"SET GLOBAL {setting}")
    return con


def execute_limited(con, sql: str, params: list | None = None) -> tuple[list[str], list[tuple]]:
    try:
        result = con.execute(sql, params)
        columns = [column[0] for column in result.description]
        rows = result.fetchmany(ROW_LIMIT + 1)
    except duckdb.InterruptException as exc:
        raise LimitError(f"the query ran past the time limit of {TIME_LIMIT_MS / 1000:g} seconds") from exc
    if len(rows) > ROW_LIMIT:
        raise LimitError(f"the result has more than {ROW_LIMIT:,} rows; ask for a coarser grain or a smaller scope")
    return columns, rows


def query(request: MetricRequest | dict, con, catalog: Catalog) -> QueryResult:
    """The single entry point: validate, compile, execute within the limits, return the rows with the SQL."""
    if isinstance(request, dict):
        try:
            request = MetricRequest.model_validate(request)
        except ValidationError as exc:
            raise RequestError(
                "; ".join(f"{'.'.join(map(str, error['loc'])) or 'request'}: {error['msg']}" for error in exc.errors())
            ) from None
    compiled = compile(request, catalog)
    columns, rows = execute_limited(con, compiled.sql, compiled.params)
    return QueryResult(columns, rows, compiled.sql, compiled.params)


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
