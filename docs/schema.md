# Plant database schema

This is the reference for writing SQL against the plant database. The database is DuckDB.
It holds one year of 15-minute energy readings for one manufacturing plant: a consumption
meter tree (the plant, its departments, their lines and their machines), one solar array, one
battery, a three-band electricity tariff and the production orders run on each line.

## How queries run

The `run_sql` tool runs one query and returns its rows.

- Only SELECT queries run: a plain SELECT, a set operation of SELECTs (UNION, UNION ALL,
  INTERSECT, EXCEPT), or `WITH ... SELECT`. Every CTE must itself be a SELECT. Anything else
  is refused with a message: INSERT, UPDATE, DELETE, CREATE, COPY, ATTACH, SET, INSTALL,
  PRAGMA, CALL, EXPLAIN, DESCRIBE, SHOW, VALUES on its own, and more than one statement.
  A comment after the final semicolon is fine.
- PIVOT and UNPIVOT work only inside a subquery: `SELECT * FROM (PIVOT ...)`.
- The database is read-only and the engine cannot read or write any other file. Use the
  tables below and table functions that generate data, such as `range`, only.
- A result may have at most 5,000 rows, and a query may run for at most 10 seconds. Past
  either limit the call fails with a message saying so. Aggregate in SQL rather than fetching
  raw readings.
- Functions and catalog views that show the engine's settings, files or internals are not
  available: any function or view whose name starts with `duckdb_`, `pragma_` or `pg_`, and
  `current_setting`, `which_secret`, `query` and `query_table`. The `information_schema`
  views work.
- The SQL dialect is DuckDB's. Engine errors are returned with the engine's message.

## Questions with no answer

Some questions cannot be answered from this data, and the sections below say which. When a
question has no answer, do not return rows for it. An empty result, a NULL or a zero is not
an answer to such a question, and neither is a partial result.

## Dataset range

The data starts on 2025-11-01 and ends before 2026-11-01, in plant local dates: the last day
with data is 2026-10-31. `dataset_info.first_date` and `dataset_info.end_date` hold these two
dates; `end_date` is excluded. A question whose period starts before 2025-11-01 or ends after
2026-11-01, even partly, has no answer: a total over the part that has data is not the answer.

## Tables and columns

### meter

One row per meter, 43 in all.

| Column | Type | Meaning |
|---|---|---|
| `meter.meter_id` | VARCHAR, primary key | Meter id, for example `dept_a` or `line_b2`. Every other table refers to meters by this id. |
| `meter.name` | VARCHAR | The name people use, for example `Welding line 2` or `Oven 1`. |
| `meter.level` | VARCHAR | `site`, `department`, `line` or `machine`. NULL for the solar and battery meters. |
| `meter.parent_id` | VARCHAR | The `meter_id` of the parent meter in the consumption tree. NULL for the site meter, the solar meter and the battery meter. |
| `meter.kind` | VARCHAR | `consumption` for every meter of the tree, `generation` for the solar array, `storage` for the battery. |

### reading

One row per meter per 15-minute interval: 35,040 intervals times 43 meters. Solar and battery
readings are in this table too, with the same columns as every consumption meter.

| Column | Type | Meaning |
|---|---|---|
| `reading.meter_id` | VARCHAR | The meter, a `meter.meter_id`. |
| `reading.ts_utc` | TIMESTAMP | Start of the 15-minute interval in UTC, with no time zone attached. It is unique per meter and orders the intervals in time. |
| `reading.kwh` | DOUBLE | Energy in the interval, in kWh. Consumption and solar values are zero or positive. Battery values are signed, see below. The average power over the interval in kW is `kwh * 4`. |
| `reading.local_start` | VARCHAR | Start of the interval in plant local time (Europe/Rome), ISO 8601 text with the UTC offset, for example `2026-10-25T02:15:00+01:00`. This is the 15-minute bucket. |
| `reading.hour_start` | VARCHAR | Start of the local hour that contains the interval, same text form, for example `2026-10-25T02:00:00+01:00`. |
| `reading.day_start` | VARCHAR | Local midnight that starts the interval's local day, same text form, for example `2026-10-25T00:00:00+02:00`. |
| `reading.month_start` | VARCHAR | Local midnight of the first day of the interval's local month, same text form, for example `2026-10-01T00:00:00+02:00`. |
| `reading.local_date` | DATE | The interval's local calendar date. Use it to filter periods. |
| `reading.tariff_band` | VARCHAR | The interval's tariff band, `F1`, `F2` or `F3`, already worked out from local time. |

The four `*_start` columns are ready-made text buckets. To group by local 15 minutes, hour,
day or month, group by `local_start`, `hour_start`, `day_start` or `month_start` and return
that column as it is; no time-zone conversion or string building is needed. The UTC offset
inside the text is the offset in force at that bucket's start (`+01:00` in winter, `+02:00`
in summer), so on the autumn clock-change day the local hour 02:00 occurs twice and appears as
two distinct buckets, `2026-10-25T02:00:00+02:00` and then `2026-10-25T02:00:00+01:00`. These
strings do not sort in time order across a clock change, so order bucketed results by the
earliest instant of each bucket, `ORDER BY min(ts_utc)`, not by the text.

### tariff

| Column | Type | Meaning |
|---|---|---|
| `tariff.tariff_band` | VARCHAR, primary key | `F1`, `F2` or `F3`; join it to `reading.tariff_band`. |
| `tariff.price_eur_per_kwh` | DOUBLE | Price in euro per kWh, fixed for the whole dataset: F1 0.28, F2 0.24, F3 0.20. |

The bands are defined by local time, from the local hour and the day of the week. Public
holidays are ignored.

| Band | Monday to Friday | Saturday | Sunday |
|---|---|---|---|
| F1 | 08:00 to 19:00 | never | never |
| F2 | 07:00 to 08:00 and 19:00 to 23:00 | 07:00 to 23:00 | never |
| F3 | 00:00 to 07:00 and 23:00 to 24:00 | 00:00 to 07:00 and 23:00 to 24:00 | all day |

Never recompute the band from `ts_utc`: use `reading.tariff_band`.

### production_order

One row per production order, about 10,500 in all.

| Column | Type | Meaning |
|---|---|---|
| `production_order.order_id` | INTEGER, primary key | Order number. |
| `production_order.line_id` | VARCHAR | The line that ran the order, a `meter_id` at level `line`. |
| `production_order.product` | VARCHAR | Product name, for example `Bracket` or `Door`. |
| `production_order.start_utc` | TIMESTAMP | Start of the order in UTC, on a 15-minute boundary. |
| `production_order.end_utc` | TIMESTAMP | End of the order in UTC, on a 15-minute boundary, excluded. The order covers the readings with `start_utc <= ts_utc < end_utc`. |
| `production_order.units` | INTEGER | Units produced by the whole order. |

Two orders never overlap on one line, so joining a line's readings to its orders on the time
range matches each reading to at most one order and cannot duplicate kWh. An order may cross
a day or month boundary.

### dataset_info

One row.

| Column | Type | Meaning |
|---|---|---|
| `dataset_info.seed` | INTEGER | Seed the dataset was generated from. |
| `dataset_info.timezone` | VARCHAR | The plant's time zone, `Europe/Rome`. |
| `dataset_info.first_date` | DATE | First local date with data, 2025-11-01. |
| `dataset_info.end_date` | DATE | Local date the data stops before, 2026-11-01, excluded. |

## The meter tree

This tree is taken from the `meter` table. Each line shows the meter id, its name and its
level (or its kind for solar and battery).

```
site (Plant, site)
  dept_a (Stamping, department)
    line_a1 (Stamping line 1, line)
      mach_a1_1 (Press 1, machine)
      mach_a1_2 (Press 2, machine)
      mach_a1_3 (Shear 1, machine)
    line_a2 (Stamping line 2, line)
      mach_a2_1 (Press 3, machine)
      mach_a2_2 (Shear 2, machine)
      mach_a2_3 (Shear 3, machine)
    line_a3 (Stamping line 3, line)
      mach_a3_1 (Press 4, machine)
      mach_a3_2 (Press 5, machine)
  dept_b (Welding, department)
    line_b1 (Welding line 1, line)
      mach_b1_1 (Welder 1, machine)
      mach_b1_2 (Welder 2, machine)
      mach_b1_3 (Robot 1, machine)
    line_b2 (Welding line 2, line)
      mach_b2_1 (Welder 3, machine)
      mach_b2_2 (Robot 2, machine)
      mach_b2_3 (Robot 3, machine)
    line_b3 (Welding line 3, line)
      mach_b3_1 (Welder 4, machine)
      mach_b3_2 (Robot 4, machine)
  dept_c (Painting, department)
    line_c1 (Painting line 1, line)
      mach_c1_1 (Oven 1, machine)
      mach_c1_2 (Sprayer 1, machine)
      mach_c1_3 (Sprayer 2, machine)
    line_c2 (Painting line 2, line)
      mach_c2_1 (Oven 2, machine)
      mach_c2_2 (Sprayer 3, machine)
  dept_d (Assembly, department)
    line_d1 (Assembly line 1, line)
      mach_d1_1 (Conveyor 1, machine)
      mach_d1_2 (Robot 5, machine)
      mach_d1_3 (Tester 1, machine)
    line_d2 (Assembly line 2, line)
      mach_d2_1 (Conveyor 2, machine)
      mach_d2_2 (Tester 2, machine)
battery (Battery, storage)
solar (Solar array, generation)
```

The site meter `site` is the plant's total load. Every parent meter reads between 3 and 8
percent more than the sum of its children, in every 15-minute interval, because it also
measures unmetered load and losses. The margin differs from parent to parent.

These 43 meters are all the meters there are. A meter, department, line or machine that is
not in the `meter` table does not exist, and a question about it has no answer: an empty or
NULL sum is not zero consumption.

The solar array (`solar`, kind `generation`) and the battery (`battery`, kind `storage`) have
no level and no parent and are not part of the consumption tree. Their readings are in the
`reading` table like any other meter's. Solar kWh is zero or positive. Battery kWh is signed:
positive while the battery discharges, negative while it charges. The battery's energy
discharged over a period is the sum of its positive readings, and the energy charged is minus
the sum of its negative readings. The site meter measures the plant's load only: solar output
and battery flows are not subtracted from it or added to it.

## Metrics

These are the quantities questions usually ask for, with the rules that define them.

- Consumption in kWh: the sum of `kwh` over the period.
- Peak demand in kW: the highest coincident average demand over a 15-minute interval (R3).
- Cost in EUR: each interval's kWh times the price of its tariff band, summed (R4).
- Energy per unit in kWh per unit: a line's kWh divided by the units it produced (R5, R6).

## Rules

**R1.** A parent meter's reading already includes all of its children, plus unmetered load
and losses. In SQL, never add a meter's readings to those of one of its ancestors: a
department's kWh already contains every one of its lines and machines, and the site's kWh
already contains every department.

**R2.** A node's total is the node's own meter, never a sum of a lower level. For the total of
the plant, filter on `meter_id = 'site'`; for a department, filter on the department's own
`meter_id` (for example `dept_b`) and do not add its lines; for a line, filter on the line's
own `meter_id` and do not add its machines. Summing the departments, lines or machines
undercounts the parent by its 3 to 8 percent margin. When a question asks for a breakdown,
return one row per meter of the requested level under the node (for example one row per
machine of a department, from each machine's own meter) and do not add the rows together. A
breakdown must be by a level below the node's own level; any other breakdown has no answer.
When a question names several meters to be added (for example two lines), add their own
meters. When one meter in such a list is an ancestor of another, or a meter is repeated, the
sum is not defined and the question has no answer: adding a department and one of its own
lines would count that line twice, because the department's meter already includes it.

**R3.** Peak demand is coincident: for each `ts_utc`, sum the `kwh` of the meters in scope and
multiply by 4 to get kW, then take the maximum over the period. For a single meter this is
`max(kwh) * 4`. For several meters, first `GROUP BY ts_utc` with `sum(kwh) * 4`, then take
`max` of that in an outer query; never add each meter's own peak. For a peak per bucket (for
example per day), group the inner query by the bucket column and `ts_utc`, and take the
maximum per bucket in the outer query. Peak demand is not split by tariff band; a question
asking for a peak per band has no answer.

**R4.** Cost prices each interval's kWh at the tariff of that interval's band: join
`tariff` on `tariff.tariff_band = reading.tariff_band` and take
`sum(reading.kwh * tariff.price_eur_per_kwh)`. Never multiply a total kWh by an average
price. A cost split by band groups by `reading.tariff_band`.

**R5.** Energy per unit is a line's own kWh divided by the units produced on it, as a ratio of
totals, never an average of per-order or per-day ratios. An order's units are spread evenly
over its 15-minute intervals, so each interval of an order carries
`units / (epoch(end_utc - start_utc) / 900)` units, and a period or bucket boundary that cuts
an order takes only its share. The kWh is all of the line's kWh in the period, including the
intervals when no order runs. In SQL: `FROM reading r LEFT JOIN production_order o ON
o.line_id = r.meter_id AND r.ts_utc >= o.start_utc AND r.ts_utc < o.end_utc`, filter
`r.meter_id` on the line, and take `sum(r.kwh) / sum(o.units / (epoch(o.end_utc -
o.start_utc) / 900))`. For a department or the plant, compute it per line (group by
`r.meter_id`) over the lines of that department or of the plant. It is not defined for a
department, the site or a machine as a whole, for several lines added together, or split by
tariff band; such a question has no answer.

**R6.** Energy per unit is null where no units were produced, for example on a day the line
did not run. Write the denominator as `NULLIF(sum(...), 0)` so the value is NULL rather than
an error or infinity, and report it as null rather than zero.

**R7.** The four metrics above read consumption meters only. Questions about plant, department,
line or machine consumption, peak, cost or energy per unit never include the `solar` or
`battery` meters. Cost prices the plant's load at the tariff and ignores solar and battery.
Consumption, peak, cost and energy per unit are not defined for `solar` or `battery`, alone
or added to consumption meters; such a question has no answer.

**R8.** `reading.ts_utc` is the UTC start of each 15-minute interval, with no time zone
attached. Do not filter or group days, months or tariff bands on `ts_utc`, and do not convert
it to local time yourself; use it only to order intervals in time and to match readings to
production orders, whose `start_utc` and `end_utc` are UTC too.

**R9.** Days, months and tariff bands use the plant's local time, through the ready-made
columns: `local_date` to filter, `local_start`, `hour_start`, `day_start` or `month_start` to
group, and `tariff_band` for the band. This holds on the two clock-change days: the spring day
2026-03-29 has 92 intervals and 23 hours, and the autumn day 2026-10-25 has 100 intervals and
25 hours. Grouping that day by `hour_start` gives 25 rows.

**R10.** A period includes its start date and excludes its end date, in plant local dates.
Filter with `local_date >= DATE 'start' AND local_date < DATE 'end'`. For example, October
2026 is `local_date >= DATE '2026-10-01' AND local_date < DATE '2026-11-01'`, and the single day
2026-10-25 is `local_date >= DATE '2026-10-25' AND local_date < DATE '2026-10-26'`.
