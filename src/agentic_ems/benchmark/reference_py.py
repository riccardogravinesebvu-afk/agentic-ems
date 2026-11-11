"""The second computation of every answer: plain Python over raw rows.

SQL here only fetches plain rows, filtered by meter and period; every sum, maximum, join,
spreading of units and ratio is done in Python, from the question and docs/schema.md alone.
"""

from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, NamedTuple

from agentic_ems.semantic import Catalog

STEP = timedelta(minutes=15)
BUCKETS = ("local_start", "hour_start", "day_start", "month_start")


class Reading(NamedTuple):
    meter_id: str
    ts_utc: datetime
    kwh: float
    local_start: str
    hour_start: str
    day_start: str
    month_start: str
    local_date: date
    tariff_band: str


Result = tuple[list[str], list[tuple]]


@dataclass
class Data:
    cursor: Any
    catalog: Catalog

    def readings(self, meters: Iterable[str], start: date, end: date) -> list[Reading]:
        rows: list[Reading] = []
        for meter_id in meters:  # one statement per meter keeps each well inside the time limit
            rows += map(Reading._make, self.cursor.execute(
                f"SELECT {', '.join(Reading._fields)} FROM reading WHERE meter_id IN (?) "
                "AND local_date >= ? AND local_date < ?",
                [meter_id, start, end],
            ).fetchall())
        return rows

    def prices(self) -> dict[str, float]:
        return dict(self.cursor.execute("SELECT tariff_band, price_eur_per_kwh FROM tariff").fetchall())

    def units(self, lines: Iterable[str], product: str | None = None) -> dict[tuple[str, datetime], float]:
        """Units per (line, interval start): each order's units spread evenly over its intervals."""
        spread: dict[tuple[str, datetime], float] = {}
        for line_id in lines:
            orders = self.cursor.execute(
                "SELECT product, start_utc, end_utc, units FROM production_order WHERE line_id IN (?)", [line_id]
            ).fetchall()
            for name, start, end, units in orders:
                if product is not None and name != product:
                    continue
                count = (end - start) // STEP
                for i in range(count):
                    spread[(line_id, start + i * STEP)] = units / count
        return spread

    def meter(self, name: str) -> str:
        [meter_id] = [m.id for m in self.catalog.meters.values() if m.name == name]
        return meter_id

    def under(self, node: str, level: str) -> list[str]:
        """Every meter at `level` below `node` in the consumption tree."""
        found = []
        for meter in self.catalog.meters.values():
            parent = meter.parent_id
            while parent is not None and parent != node:
                parent = self.catalog.meters[parent].parent_id
            if parent == node and meter.level == level:
                found.append(meter.id)
        return sorted(found)


def month(year: int, number: int) -> tuple[date, date]:
    return date(year, number, 1), date(year + number // 12, number % 12 + 1, 1)


def kwh(cell: list[Reading]) -> float:
    return sum(r.kwh for r in cell)


def peak(cell: list[Reading]) -> float:
    """Coincident peak: per interval sum the meters' kWh, times 4, then the maximum."""
    per_interval: dict[datetime, float] = defaultdict(float)
    for r in cell:
        per_interval[r.ts_utc] += r.kwh
    return max(per_interval.values()) * 4


def ratio(numerator: float, denominator: float) -> float | None:
    return None if denominator == 0 else numerator / denominator


def answer(rows: list[Reading], keys: list[str], value: Callable[[list[Reading]], Any], bucket: str = "") -> Result:
    """Group rows into one cell per key, compute `value` per cell, return the canonical shape and order."""
    assert ("bucket_start" in keys) == (bucket in BUCKETS)
    cells: dict[tuple, list[Reading]] = defaultdict(list)
    for r in rows:
        parts = {"meter_id": r.meter_id, "bucket_start": getattr(r, bucket, None), "tariff_band": r.tariff_band}
        cells[tuple(parts[k] for k in keys)].append(r)
    return [*keys, "value"], ordered(keys, [(*key, value(cell)) for key, cell in cells.items()])


def ordered(keys: list[str], rows: list[tuple]) -> list[tuple]:
    def sort_key(row: tuple) -> tuple:
        return tuple(datetime.fromisoformat(v) if k == "bucket_start" else v for k, v in zip(keys, row))

    return sorted(rows, key=sort_key)


def single(value: float | None) -> Result:
    return ["value"], [(value,)]


def cost(data: Data) -> Callable[[list[Reading]], float]:
    prices = data.prices()
    return lambda cell: sum(r.kwh * prices[r.tariff_band] for r in cell)


def per_unit(data: Data, lines: list[str]) -> Callable[[list[Reading]], float | None]:
    spread = data.units(lines)
    return lambda cell: ratio(kwh(cell), sum(spread.get((r.meter_id, r.ts_utc), 0.0) for r in cell))


def consumption(data: Data, meters: list[str], period: tuple[date, date], keys=(), value=kwh, bucket="") -> Result:
    return answer(data.readings(meters, *period), list(keys), value, bucket)


# ---------------------------------------------------------------- out of scope


def o2(d: Data) -> Result:
    return single(-kwh(d.readings(["battery"], date(2026, 4, 27), date(2026, 5, 4))))


def o3(d: Data) -> Result:
    return single(100 * kwh(d.readings(["solar"], *month(2026, 7))) / kwh(d.readings(["site"], *month(2026, 7))))


def o4(d: Data) -> Result:
    columns, rows = consumption(d, d.under("site", "machine"), month(2026, 2), ["meter_id"])
    return columns, sorted(sorted(rows, key=lambda row: row[1], reverse=True)[:5])


def o5(d: Data) -> Result:
    assembly = d.meter("Assembly")
    return single(kwh(d.readings([assembly], *month(2026, 3))) - kwh(d.readings([assembly], *month(2026, 2))))


def o6(d: Data) -> Result:
    night = [r.kwh * 4 for r in d.readings(["site"], *month(2025, 11)) if int(r.local_start[11:13]) < 5]
    return single(sum(night) / len(night))


def o7(d: Data) -> Result:
    lines = d.under(d.meter("Stamping"), "line")
    spread = d.units(lines, "Hinge")
    return single(sum(spread.get((r.meter_id, r.ts_utc), 0.0) for r in d.readings(lines, *month(2026, 5))))


def o8(d: Data) -> Result:
    rows = d.readings([d.meter("Welding line 2")], *month(2026, 10))
    return single(kwh([r for r in rows if r.local_date.weekday() >= 5]))


def o9(d: Data) -> Result:
    period = month(2026, 8)
    return single(kwh(d.readings(["site"], *period)) - kwh(d.readings(d.under("site", "department"), *period)))


def o10(d: Data) -> Result:
    period = month(2025, 12)
    site = kwh(d.readings(["site"], *period))
    return consumption(d, d.under("site", "department"), period, ["meter_id"], lambda cell: 100 * kwh(cell) / site)


def o11(d: Data) -> Result:
    return single(sum(r.kwh * 4 > 1500 for r in d.readings(["site"], *month(2026, 5))) / 4)


def o12(d: Data) -> Result:
    lines = d.under(d.meter("Painting"), "line")
    spread = d.units(lines, "Hood")
    hood = [r for r in d.readings(lines, *month(2026, 3)) if (r.meter_id, r.ts_utc) in spread]
    return single(ratio(kwh(hood), sum(spread[(r.meter_id, r.ts_utc)] for r in hood)))


def o13(d: Data) -> Result:
    daily: dict[date, float] = defaultdict(float)
    for r in d.readings([d.meter("Painting line 2")], *month(2026, 4)):
        daily[r.local_date] += r.kwh
    assert len(daily) == 30
    return single(sum(daily.values()) / len(daily))


def o14(d: Data) -> Result:
    weeks: dict[date, float] = defaultdict(float)
    monday_start: dict[date, str] = {}
    for r in d.readings(["site"], date(2025, 12, 8), date(2026, 1, 19)):
        monday = r.local_date - timedelta(days=r.local_date.weekday())
        weeks[monday] += r.kwh
        if r.local_date == monday:
            monday_start[monday] = r.day_start  # local midnight of that Monday, with its offset
    return ["bucket_start", "value"], ordered(["bucket_start"], [(monday_start[m], v) for m, v in weeks.items()])


# One computation per answerable question id. Meters are found by the names the question uses.
ANSWERS: dict[str, Callable[[Data], Result]] = {
    "H1": lambda d: consumption(d, ["site"], month(2026, 2)),
    "H2": lambda d: consumption(d, [d.meter("Assembly")], (date(2026, 4, 13), date(2026, 4, 20))),
    "H3": lambda d: consumption(d, [d.meter("Stamping line 2")], (date(2026, 6, 4), date(2026, 6, 5))),
    "H4": lambda d: consumption(d, d.under("site", "department"), month(2025, 11), ["meter_id"]),
    "H5": lambda d: consumption(d, d.under(d.meter("Stamping"), "line"), month(2026, 5), ["meter_id"]),
    "H6": lambda d: consumption(
        d, d.under(d.meter("Welding line 2"), "machine"), (date(2026, 9, 21), date(2026, 9, 28)), ["meter_id"]
    ),
    "H7": lambda d: consumption(d, d.under(d.meter("Assembly"), "machine"), month(2026, 7), ["meter_id"]),
    "H8": lambda d: consumption(d, ["site"], month(2025, 12), value=peak),
    "H9": lambda d: consumption(d, [d.meter("Painting")], (date(2025, 11, 10), date(2025, 11, 17)), value=peak),
    "H10": lambda d: consumption(
        d, [d.meter("Welding line 1"), d.meter("Painting line 2")], (date(2026, 3, 9), date(2026, 3, 16)),
        value=peak,
    ),
    "H11": lambda d: consumption(d, ["site"], month(2026, 8), value=cost(d)),
    "H12": lambda d: consumption(d, d.under("site", "department"), month(2026, 1), ["meter_id"], cost(d)),
    "H13": lambda d: consumption(d, [d.meter("Stamping line 1"), d.meter("Stamping line 3")], month(2026, 9)),
    "H14": lambda d: consumption(
        d, ["site"], (date(2026, 6, 15), date(2026, 6, 22)), ["bucket_start"], bucket="day_start"
    ),
    "H15": lambda d: consumption(
        d, d.under("site", "department"), (date(2026, 7, 1), date(2026, 10, 1)), ["meter_id", "bucket_start"],
        bucket="month_start",
    ),
    "H16": lambda d: consumption(d, ["site"], month(2026, 10), ["tariff_band"]),
    "H17": lambda d: consumption(d, [d.meter("Welding")], month(2025, 12), ["tariff_band"], cost(d)),
    "H18": lambda d: consumption(
        d, ["site"], (date(2026, 3, 18), date(2026, 3, 19)), ["bucket_start"], bucket="hour_start"
    ),
    # The department's reading already contains its line 2.
    "H19": lambda d: consumption(d, [d.meter("Painting")], month(2026, 1)),
    # The department's reading already contains its lines and machines.
    "H20": lambda d: consumption(d, [d.meter("Welding")], month(2026, 4)),
    "H21": lambda d: consumption(
        d, ["site"], (date(2026, 2, 11), date(2026, 2, 12)), ["bucket_start"], lambda cell: kwh(cell) * 4,
        "local_start",
    ),
    "C1": lambda d: consumption(d, [d.meter("Oven 2")], (date(2026, 1, 27), date(2026, 1, 28))),
    "C2": lambda d: consumption(d, [d.meter("Welder 3")], (date(2026, 10, 5), date(2026, 10, 12)), value=peak),
    "C3": lambda d: consumption(
        d, [d.meter("Press 4")], (date(2026, 4, 8), date(2026, 4, 9)), ["bucket_start"], bucket="hour_start"
    ),
    "C4": lambda d: consumption(d, [d.meter("Tester 1")], month(2026, 9), ["tariff_band"]),
    "C5": lambda d: consumption(d, [d.meter("Robot 4")], month(2025, 11), value=cost(d)),
    "C6": lambda d: consumption(
        d, [d.meter("Shear 2")], (date(2026, 2, 1), date(2026, 5, 1)), ["bucket_start"], bucket="month_start"
    ),
    "C7": lambda d: consumption(d, [d.meter("Press 3")], month(2026, 8)),
    "M1": lambda d: consumption(
        d, [d.meter("Welding line 1")], month(2026, 10), value=per_unit(d, [d.meter("Welding line 1")])
    ),
    "M2": lambda d: consumption(
        d, d.under("site", "line"), month(2026, 6), ["meter_id"], per_unit(d, d.under("site", "line"))
    ),
    "M3": lambda d: consumption(
        d, [d.meter("Stamping line 3")], (date(2026, 2, 16), date(2026, 2, 23)), ["bucket_start"],
        per_unit(d, [d.meter("Stamping line 3")]), "day_start",
    ),
    "M4": lambda d: consumption(
        d, [d.meter("Robot 2")], (date(2026, 9, 10), date(2026, 9, 11)), ["bucket_start"], bucket="local_start"
    ),
    "M5": lambda d: consumption(
        d, [d.meter("Painting line 1")], (date(2026, 10, 25), date(2026, 10, 26)), ["bucket_start"],
        bucket="hour_start",
    ),
    "M6": lambda d: consumption(d, [d.meter("Assembly line 2")], (date(2026, 5, 11), date(2026, 5, 25))),
    "O1": lambda d: consumption(d, ["solar"], month(2026, 6)),
    "O2": o2,
    "O3": o3,
    "O4": o4,
    "O5": o5,
    "O6": o6,
    "O7": o7,
    "O8": o8,
    "O9": o9,
    "O10": o10,
    "O11": o11,
    "O12": o12,
    "O13": o13,
    "O14": o14,
}
