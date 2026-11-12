# Benchmark answer set: how it was built and what it is worth

The benchmark has 47 answerable questions in four groups (21 hierarchy, 7 control, 6 metric
definition, 13 out of scope) and 8 unanswerable ones, in `src/agentic_ems/benchmark/questions.toml`.
This page says how the questions and their answers were made, what was checked, and what was not.

## How the set was built

- The question types were drafted from public practice: the energy performance indicators of
  ISO 50001 and ISO 50006, and the questions an energy management system is asked every day.
  Eight types were added from a list of generic error classes prepared in a separate session
  and reviewed by the owner; those questions record `error class Pn` as their source.
- The wording of each question was written by an agent to resemble how plant staff ask.
  The questions are not logs of real users.
- One second reader reviewed the whole set on 2026-11-11. Questions that allowed a second
  reasonable reading were reworded or rejected; rejected questions stay in the file under
  `[[rejected]]` with their reason. `docs/review-log.md` records what was read, changed and
  rejected, with dates.

## What was verified, and how

- **Two computations of every answer.** Each answerable question has a hand-written reference
  query (`reference_sql.toml`) and a second computation in plain Python over the raw rows
  (`reference_py.py`). The two were written from the same specification (the question,
  `docs/schema.md` and the design) by two separate writers who did not see each other's work;
  the 2026-11-11 entry of `docs/review-log.md` records what the second writer could see.
  The two were kept apart by instruction in a shared working tree, not by a technical barrier.
  One command, `uv run python -m agentic_ems.benchmark.verify`, rebuilds the database from the
  seed, runs both and compares them row by row with the scorer's tolerance (0.1% relative,
  0.001 absolute when the reference is zero). It runs offline with no API key.
- **Known wrong answers.** Each hierarchy question lists the row sets a mistaken aggregation
  gives (`double_count`, `children_sum`, `separate_peaks`, `extra_rows`). A test checks that
  every one differs from its reference by more than the tolerance.
- **Totals known by construction.** The generator draws each parent's margin from its seed, so
  for one day the site's kWh must equal the sum of the machines' kWh times the product of the
  margins on each machine's path to the site; a test asserts it. `tests/test_generator.py`
  asserts that every parent reads 3% to 8% above its children in every interval, that orders
  never overlap on a line, and that the clock-change days have 92 and 100 intervals.

## What was not verified

- Both computations share one specification. A rule misread in the same way by both writers,
  or a mistake in the specification itself, is not caught by the comparison.
- The owner checked ten of the 47 reference answers on 2026-11-12 (`docs/review-log.md`). The
  owner read the values and did not recompute them; the other 37 were not hand-checked.
- The owner has not yet reworded the questions; the wording is the agent's.

## Limits

- One synthetic plant with clean data: no gaps, no counter resets, no scale factors, no
  missing or late readings.
- One model, one run date. Results from other models or dates are not covered.
- Absolute dates only. No question says "last month" or "yesterday".

## Error classes and what version 1 does with each

These are errors typical of energy analytics systems in general. Each one is either enforced
by the semantic layer, tested by a question, or left out with its reason. Question ids are the
final ids in the questions file.

| Class | Typical error | In version 1 |
|---|---|---|
| P1 | A meter summed with meters downstream of it | Layer rule and the whole hierarchy group |
| P2 | An allocation share applied to a physical reading | Out: the synthetic plant has no cost allocation |
| P3 | Different quantities (energy, water, gas) in one sum | Partly: only electricity exists; U3 |
| P4 | Consumption confused with generation or net balance | O1, O3 |
| P5 | A generation-only meter queried as consumption | Layer rule (rejected with a message, never filtered silently) and U5 |
| P6 | Daily statistics computed on finer rows | O13 |
| P7 | Power profile not summed per instant | Layer rule for peak and H21 |
| P8 | The amount above a threshold reported as the peak | Out: no threshold records in the data; O11 is the nearest question |
| P9 | Interval energy reported as power, or the reverse | Layer rule (interval kWh times 4) and C2 |
| P10 | A time window that does not meet the data | Layer rule (period outside the dataset is an error stating the range), U2, U6 |
| P11 | A partly covered period compared with a full one | U6; comparisons themselves are out of scope (O5) |
| P12 | Ambiguous week boundaries | O14 |
| P13 | UTC instants grouped without conversion to local time | Layer rule (local-time columns), C6, M5, H16 |
| P14 | A correct number described with the wrong grain | Demo: the headline is rendered by code from the rows; prose is never scored |
| P15 | A total rebuilt from a truncated result | Layer rule: the row limit is an error, never a truncation |
| P16 | Cost, emissions or thresholds from coefficients that are not in the data | Layer rule (cost uses the tariff table), U4, U7 |
| P17 | An unrecognised name reported as zero consumption | Layer rule (unknown meter is an error), U1, C7 |
| P18 | Consumption lost through an incomplete link to a department | Out: the synthetic tree has one parent link and it is complete |
| P19 | Scope narrowed or widened by mistake | U8 |
| P20 | Counter resets, gaps and scale factors ignored | Out: the generator writes clean interval energy; stated under Limits above |
