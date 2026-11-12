"""Seeded generator for the plant database: meters, 15-minute readings, tariff and orders.

Battery kWh is signed: positive while discharging, negative while charging.
"""

import csv
import math
import random
import tempfile
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import duckdb

TIMEZONE = "Europe/Rome"
TZ = ZoneInfo(TIMEZONE)
STEP = timedelta(minutes=15)
CLOSURES = [(date(2025, 12, 24), date(2026, 1, 6)), (date(2026, 8, 8), date(2026, 8, 23))]
PRICES = {"F1": 0.28, "F2": 0.24, "F3": 0.20}

# Machine type: (working kW, standby kW).
POWER = {
    "Press": (160, 12),
    "Shear": (70, 5),
    "Welder": (90, 6),
    "Robot": (35, 4),
    "Oven": (220, 40),
    "Sprayer": (45, 4),
    "Conveyor": (18, 3),
    "Tester": (12, 3),
}

# Department: name, products, and per line its machine types, weekday shifts as local
# (start hour, hours long), and whether its first shift also runs on Saturday morning.
# Every line has its own shift set, so no two lines peak in the same interval.
DEPARTMENTS = {
    "a": ("Stamping", ["Bracket", "Panel", "Hinge"], [
        (["Press", "Press", "Shear"], [(6, 8)], True),
        (["Press", "Shear", "Shear"], [(6, 8), (14, 8)], False),
        (["Press", "Press"], [(6, 8), (14, 8), (22, 8)], False),
    ]),
    "b": ("Welding", ["Frame", "Subframe", "Crossbeam"], [
        (["Welder", "Welder", "Robot"], [(14, 8)], False),
        (["Welder", "Robot", "Robot"], [(5, 8), (13, 8), (21, 8)], True),
        (["Welder", "Robot"], [(7, 8), (15, 8)], False),
    ]),
    "c": ("Painting", ["Door", "Hood"], [
        (["Oven", "Sprayer", "Sprayer"], [(6, 9), (15, 7)], True),
        (["Oven", "Sprayer"], [(8, 8)], False),
    ]),
    "d": ("Assembly", ["Cabinet", "Housing"], [
        (["Conveyor", "Robot", "Tester"], [(7, 9), (16, 6)], True),
        (["Conveyor", "Tester"], [(13, 8)], False),
    ]),
}


def band(local: datetime) -> str:
    weekday, hour = local.weekday(), local.hour
    if weekday < 5 and 8 <= hour < 19:
        return "F1"
    if (weekday < 5 and (hour == 7 or 19 <= hour < 23)) or (weekday == 5 and 7 <= hour < 23):
        return "F2"
    return "F3"


def closed(day: date) -> bool:
    return day.weekday() == 6 or any(first <= day <= last for first, last in CLOSURES)


def build_meters() -> tuple[list[tuple], dict]:
    """Meter rows and the line plan: line id -> (products, machines, shifts, saturday)."""
    meters: list[tuple] = [("site", "Plant", "site", None, "consumption")]
    lines = {}
    numbers = dict.fromkeys(POWER, 0)
    for d, (dept_name, products, plan) in DEPARTMENTS.items():
        meters.append((f"dept_{d}", dept_name, "department", "site", "consumption"))
        for n, (types, shifts, saturday) in enumerate(plan, 1):
            line_id = f"line_{d}{n}"
            meters.append((line_id, f"{dept_name} line {n}", "line", f"dept_{d}", "consumption"))
            machines = []
            for m, kind in enumerate(types, 1):
                numbers[kind] += 1
                machine_id = f"mach_{d}{n}_{m}"
                meters.append((machine_id, f"{kind} {numbers[kind]}", "machine", line_id, "consumption"))
                machines.append((machine_id, kind))
            lines[line_id] = (products, machines, shifts, saturday)
    meters += [("solar", "Solar array", None, None, "generation"), ("battery", "Battery", None, None, "storage")]
    return meters, lines


def build_orders(rng: random.Random, lines: dict, start: date, end: date) -> list[tuple]:
    """Orders fill each shift window back to back with short changeovers, on 15-minute steps."""
    orders = []
    for line_id, (products, _, shifts, saturday) in lines.items():
        day = start
        while day < end:
            windows = shifts if day.weekday() < 5 else shifts[:1] if day.weekday() == 5 and saturday else []
            for hour, length in windows:
                begin = datetime.combine(day, time(hour), TZ)
                finish = begin + timedelta(hours=length)
                last_day = (finish - STEP).date()
                if closed(day) or closed(last_day) or last_day >= end:
                    continue
                cursor = begin.astimezone(UTC)
                stop = finish.astimezone(UTC)
                while stop - cursor >= timedelta(hours=1):
                    quarters = rng.randint(8, 20)
                    order_end = min(cursor + quarters * STEP, stop)
                    if stop - order_end < timedelta(hours=1):
                        order_end = stop
                    units = rng.randint(20, 40) * (order_end - cursor) // timedelta(hours=1)
                    orders.append([line_id, rng.choice(products), cursor, order_end, units])
                    cursor = order_end + rng.randint(1, 2) * STEP
            day += timedelta(days=1)
    orders.sort(key=lambda o: (o[2], o[0]))
    return [(i, *o) for i, o in enumerate(orders, 1)]


def solar_kwh(rng: random.Random, utc: datetime, cloud: float) -> float:
    season = math.sin(2 * math.pi * (utc.timetuple().tm_yday - 80) / 365)
    half_day = 6 + 1.6 * season
    solar_hour = utc.hour + utc.minute / 60 + 7.5 / 60 + 0.8  # interval midpoint, plant longitude ~12.5 E
    x = (solar_hour - 12) / half_day
    if abs(x) >= 1:
        return 0.0
    peak_kw = 300 * (0.65 + 0.35 * season) * cloud
    return round(peak_kw / 4 * math.cos(math.pi / 2 * x) ** 2 * rng.uniform(0.9, 1.0), 3)


def battery_kwh(rng: random.Random, local: datetime, solar: float, charged: float) -> float:
    """Charge from solar around midday; discharge 90% of the day's charge over 17:00-20:00."""
    if 10 <= local.hour < 15 and solar > 10:
        return -round(min(solar * 0.4, 25) * rng.uniform(0.9, 1.0), 3)
    if 17 <= local.hour < 20:
        return round(charged * 0.9 / 12 * rng.uniform(0.95, 1.05), 3)
    return 0.0


def generate(
    path: Path, seed: int = 20261111, start: date = date(2025, 11, 1), end: date = date(2026, 11, 1)
) -> None:
    rng = random.Random(seed)
    meters, lines = build_meters()
    margins = {m[0]: rng.uniform(0.03, 0.08) for m in meters if m[2] in ("site", "department", "line")}
    orders = build_orders(rng, lines, start, end)

    first = datetime.combine(start, time(), TZ).astimezone(UTC)
    count = (datetime.combine(end, time(), TZ).astimezone(UTC) - first) // STEP
    # Interval index -> (order start index, or None while no order runs), per line.
    running: dict[str, list[int | None]] = {line_id: [None] * count for line_id in lines}
    for _, line_id, _, order_start, order_end, _ in orders:
        a, b = (order_start - first) // STEP, (order_end - first) // STEP
        for i in range(a, b):
            running[line_id][i] = a

    path.parent.mkdir(parents=True, exist_ok=True)
    building = path.with_name(path.name + ".tmp")  # replaces the target only once complete
    for stale in (building, Path(f"{building}.wal")):
        stale.unlink(missing_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        intervals_csv, kwh_csv, orders_csv = (Path(tmp) / f"{n}.csv" for n in ("intervals", "kwh", "orders"))
        with intervals_csv.open("w", newline="") as fi, kwh_csv.open("w", newline="") as fk:
            iw, kw = csv.writer(fi), csv.writer(fk)
            cloud, cloud_day, charged = 1.0, None, 0.0
            for i in range(count):
                utc = first + i * STEP
                loc = utc.astimezone(TZ)
                # Rome offsets are whole hours, so the UTC hour start is the local hour start.
                hour = utc.replace(minute=0).astimezone(TZ)
                iw.writerow([
                    i, utc.strftime("%Y-%m-%d %H:%M:%S"), loc.isoformat(), hour.isoformat(),
                    datetime.combine(loc.date(), time(), TZ).isoformat(),
                    datetime(loc.year, loc.month, 1, tzinfo=TZ).isoformat(),
                    loc.date().isoformat(), band(loc),
                ])
                if loc.date() != cloud_day:
                    cloud, cloud_day, charged = rng.uniform(0.2, 1.0), loc.date(), 0.0
                totals = dict.fromkeys(margins, 0.0)
                for line_id, (_, machines, _, _) in lines.items():
                    order_start = running[line_id][i]
                    for machine_id, kind in machines:
                        work_kw, standby_kw = POWER[kind]
                        kwh = standby_kw / 4 * rng.uniform(0.9, 1.1)
                        if order_start is not None:
                            ramp = min(1.0, (i - order_start + 1) / 3)  # start-up ramp over 45 minutes
                            duty = min(1.0, max(0.4, rng.gauss(0.8, 0.1)))
                            kwh = max(kwh, work_kw / 4 * duty * ramp)
                        kwh = round(max(0.5, kwh), 3)
                        totals[line_id] += kwh
                        kw.writerow([i, machine_id, kwh])
                for parent_level in ("line", "department"):  # children before parents
                    for meter_id, _, level, parent_id, _ in meters:
                        if level == parent_level:
                            kwh = round(totals[meter_id] * (1 + margins[meter_id]), 3)
                            totals[parent_id] += kwh
                            kw.writerow([i, meter_id, kwh])
                kw.writerow([i, "site", round(totals["site"] * (1 + margins["site"]), 3)])
                solar = solar_kwh(rng, utc, cloud)
                kw.writerow([i, "solar", solar])
                battery = battery_kwh(rng, loc, solar, charged)
                charged -= min(battery, 0.0)
                kw.writerow([i, "battery", battery])
        with orders_csv.open("w", newline="") as fo:
            csv.writer(fo).writerows(
                (o[0], o[1], o[2], o[3].strftime("%Y-%m-%d %H:%M:%S"), o[4].strftime("%Y-%m-%d %H:%M:%S"), o[5])
                for o in orders
            )

        con = duckdb.connect(str(building))
        try:
            con.execute("""
                CREATE TABLE meter(meter_id VARCHAR PRIMARY KEY, name VARCHAR, level VARCHAR,
                    parent_id VARCHAR, kind VARCHAR);
                CREATE TABLE reading(meter_id VARCHAR, ts_utc TIMESTAMP, kwh DOUBLE, local_start VARCHAR,
                    hour_start VARCHAR, day_start VARCHAR, month_start VARCHAR, local_date DATE,
                    tariff_band VARCHAR);
                CREATE TABLE tariff(tariff_band VARCHAR PRIMARY KEY, price_eur_per_kwh DOUBLE);
                CREATE TABLE production_order(order_id INTEGER PRIMARY KEY, line_id VARCHAR, product VARCHAR,
                    start_utc TIMESTAMP, end_utc TIMESTAMP, units INTEGER);
                CREATE TABLE dataset_info(seed INTEGER, timezone VARCHAR, first_date DATE, end_date DATE);
            """)
            con.executemany("INSERT INTO meter VALUES (?, ?, ?, ?, ?)", meters)
            con.executemany("INSERT INTO tariff VALUES (?, ?)", list(PRICES.items()))
            con.execute("INSERT INTO dataset_info VALUES (?, ?, ?, ?)", [seed, TIMEZONE, start, end])
            con.execute(
                """
                INSERT INTO production_order SELECT * FROM read_csv(?, header = false, columns = {
                    'order_id': 'INTEGER', 'line_id': 'VARCHAR', 'product': 'VARCHAR',
                    'start_utc': 'TIMESTAMP', 'end_utc': 'TIMESTAMP', 'units': 'INTEGER'})
                """,
                [str(orders_csv)],
            )
            con.execute(
                """
                INSERT INTO reading
                SELECT k.meter_id, i.ts_utc, k.kwh, i.local_start, i.hour_start, i.day_start,
                    i.month_start, i.local_date, i.tariff_band
                FROM read_csv(?, header = false, columns = {
                    'idx': 'INTEGER', 'meter_id': 'VARCHAR', 'kwh': 'DOUBLE'}) k
                JOIN read_csv(?, header = false, columns = {
                    'idx': 'INTEGER', 'ts_utc': 'TIMESTAMP', 'local_start': 'VARCHAR',
                    'hour_start': 'VARCHAR', 'day_start': 'VARCHAR', 'month_start': 'VARCHAR',
                    'local_date': 'DATE', 'tariff_band': 'VARCHAR'}) i USING (idx)
                ORDER BY i.ts_utc, k.meter_id
                """,
                [str(kwh_csv), str(intervals_csv)],
            )
        finally:
            con.close()
    Path(f"{path}.wal").unlink(missing_ok=True)  # a stale WAL would be replayed onto the new file
    building.replace(path)


if __name__ == "__main__":
    from agentic_ems.semantic import (
        DEFAULT_DB,  # the path every reader opens, whatever the working directory
    )

    generate(DEFAULT_DB)
