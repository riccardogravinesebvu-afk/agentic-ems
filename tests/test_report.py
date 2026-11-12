import copy
import json
from pathlib import Path

import pytest

from agentic_ems.benchmark.report import (
    END,
    START,
    bootstrap,
    build_block,
    wilson,
    write_readme,
)

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "results_small.json").read_text())
BELOW_MINIMUM = "below the minimum of"
NO_DOUBLE_COUNT = "No run produced a `double_count` value."

EXPECTED = """\
**Runs whose returned rows held a wrong value** (known wrong plus other wrong, of the runs scored; the model's text is not scored): semantic 0 of 8 runs, sql 3 of 8 runs.

2 of 2 repeats completed, 4 questions, model claude-opus-5-5, effort high.

Spend: 0.22 EUR in the runs the table counts, 0.00 EUR in runs of an incomplete repeat and 0.01 EUR in attempts that were not scored, of a budget of 20.00 EUR.

| Arm | Group | Correct | Refusals | Known wrong | Other wrong | No answer | Cost EUR | EUR per correct | Earlier call correct |
|---|---|---|---|---|---|---|---|---|---|
| semantic | hierarchy | 3 of 4, 75% (30% to 95%) | 1 | 0 | 0 | 0 | 0.03 | 0.011 | 0 |
| semantic | control | 2 of 2, 100% (34% to 100%) | 0 | 0 | 0 | 0 | 0.02 | 0.008 | 0 |
| semantic | unanswerable | 2 of 2 | 2 | 0 | 0 | 0 | 0.02 | 0.008 | 0 |
| sql | hierarchy | 2 of 4, 50% (15% to 85%) | 0 | children_sum 1, double_count 1 | 0 | 0 | 0.08 | 0.040 | 1 |
| sql | control | 1 of 2, 50% (9% to 91%) | 0 | 0 | 0 | 1 | 0.04 | 0.040 | 0 |
| sql | unanswerable | 1 of 2 | 1 | 0 | 1 | 0 | 0.04 | 0.040 | 0 |

**Difference between arms**, semantic minus sql, in accuracy points, paired by question:

| Group | Difference | 95% interval |
|---|---|---|
| hierarchy | +25 | +0 to +50 |
| control | +50 | +50 to +50 |

**Example**, a double count from a recorded run (sql arm, question H1, repeat 1):

> What was the plant's total electricity consumption in February 2026?

The SQL that ran:

```sql
SELECT sum(kwh) AS value FROM reading WHERE meter_id IN ('site', 'dept_a', 'dept_b')
```

The value it returned:

```
value
104.0
```

The reference value:

```
value
100.0
```

[The transcript of this run](results/runs/20261112T090000Z/H1-sql-1.json)

Intervals: Wilson score 95% for accuracy, a 95% bootstrap over questions (10,000 resamples, fixed seed) for differences.

Known wrong values are examples, not a complete list, so their counts are lower bounds."""


def test_block_matches_the_stored_text():
    assert build_block(FIXTURE) == EXPECTED


def test_wilson_interval():
    assert wilson(4, 4) == pytest.approx((0.5101, 1.0), abs=1e-4)
    assert wilson(0, 4) == pytest.approx((0.0, 0.4899), abs=1e-4)
    assert wilson(5, 10) == pytest.approx((0.2366, 0.7634), abs=1e-4)


def test_bootstrap_interval():
    # Ten questions, one with a difference of 1: a resample's mean is k/10 with k ~ Binomial(10, 0.1).
    # P(k = 0) = 0.35 puts the 2.5% point at 0; P(k >= 3) = 0.070 and P(k >= 4) = 0.013 put the 97.5% point at 0.3.
    assert bootstrap([0.0] * 9 + [1.0]) == pytest.approx((0.0, 0.3))
    assert bootstrap([0.25] * 5) == (0.25, 0.25)


def test_below_the_minimum_shows_counts_only():
    results = copy.deepcopy(FIXTURE)
    results["min_repeats"] = 3
    block = build_block(results)
    assert f"{BELOW_MINIMUM} 3 repeats" in block and "%" not in block and "Difference" not in block


def test_an_incomplete_repeat_is_not_counted():
    results = copy.deepcopy(FIXTURE)
    results["runs"].pop()  # repeat 2 now lacks one run
    block = build_block(results)
    assert "1 of 2 repeats completed" in block and f"{BELOW_MINIMUM} 2 repeats" in block
    assert "stopped" not in block
    assert "0.11 EUR in the runs the table counts, 0.09 EUR in runs of an incomplete repeat" in block
    results["stopped"] = "the budget: example"
    assert "The benchmark stopped early, at the budget: example." in build_block(results)
    assert "semantic 0 of 4 runs, sql 3 of 4 runs" in block


def test_attempts_still_running_are_named_next_to_the_spend():
    results = copy.deepcopy(FIXTURE)
    assert "still running" not in build_block(results)
    results["attempts_still_running"] = 2
    assert ("2 abandoned attempts were still running at the final save; their spend is not included."
            in build_block(results))
    results["attempts_still_running"] = 1
    assert "1 abandoned attempt was still running" in build_block(results)


def test_without_a_double_count_the_block_says_so():
    results = copy.deepcopy(FIXTURE)
    for run in results["runs"]:
        if run["category"] == "double_count":
            run["category"] = "children_sum"
    block = build_block(results)
    assert NO_DOUBLE_COUNT in block and "transcript" not in block
    assert NO_DOUBLE_COUNT not in build_block(FIXTURE)


def test_write_readme_replaces_only_the_marked_block(tmp_path):
    readme = tmp_path / "README.md"
    readme.write_text(f"# Title\n\nBefore.\n\n{START}\nold numbers\n{END}\n\nAfter.\n")
    write_readme(readme, "new block")
    expected = f"# Title\n\nBefore.\n\n{START}\nnew block\n{END}\n\nAfter.\n"
    assert readme.read_text() == expected
    write_readme(readme, "new block")
    assert readme.read_text() == expected


def test_write_readme_fails_clearly_without_the_file_or_the_markers(tmp_path):
    readme = tmp_path / "README.md"
    with pytest.raises(SystemExit, match="does not exist"):
        write_readme(readme, "block")
    for text in ("# Title\n", f"{START}\nno end\n", f"{END}\n{START}\n"):
        readme.write_text(text)
        with pytest.raises(SystemExit, match="markers"):
            write_readme(readme, "block")
        assert readme.read_text() == text


def third_arm() -> dict:
    extra = copy.deepcopy(FIXTURE)
    extra["versions"]["prompts_sha256"] = {"sql_schema_only": "0" * 64}
    extra["runs"] = [{**run, "arm": "sql_schema_only"} for run in extra["runs"] if run["arm"] == "sql"]
    return extra


def test_a_later_run_adds_its_arm_to_the_block(tmp_path):
    from agentic_ems.benchmark.report import load_results

    (tmp_path / "schema_only").mkdir()
    (tmp_path / "results.json").write_text(json.dumps(FIXTURE))
    (tmp_path / "schema_only" / "results.json").write_text(json.dumps(third_arm()))
    merged = load_results(tmp_path)
    block = build_block(merged)
    assert "sql 3 of 8 runs, sql_schema_only 3 of 8 runs" in block
    assert block.count("**Difference between arms**") == 3 and "sql minus sql_schema_only" in block
    assert merged["budget_eur"] == 2 * FIXTURE["budget_eur"] and len(merged["runs"]) == 24
    assert merged["runs"][-1]["transcript"].startswith("schema_only/runs/")
    assert json.loads((tmp_path / "results.json").read_text()) == FIXTURE


@pytest.mark.parametrize("change", ["scorer_sha256", "questions_sha256", "model", "effort", "seed"])
def test_runs_that_differ_are_not_reported_together(change):
    from agentic_ems.benchmark.report import merge

    extra = third_arm()
    extra["versions"][change] = "other"
    with pytest.raises(SystemExit, match=change):
        merge(FIXTURE, extra, "schema_only")


def test_a_later_run_that_stopped_early_does_not_cut_the_first_run():
    from agentic_ems.benchmark.report import merge

    extra = third_arm()
    extra["runs"] = [run for run in extra["runs"] if run["repeat"] == 1]
    extra["stopped"] = "the budget"
    block = build_block(merge(FIXTURE, extra, "schema_only"))
    assert "semantic 2, sql 2, sql_schema_only 1; of 2 repeats completed" in block
    assert "semantic 0 of 8 runs, sql 3 of 8 runs, sql_schema_only" in block and "of 4 runs" in block
    assert "stopped early, at in results/schema_only, the budget" in block
    assert "0.00 EUR in runs of an incomplete repeat" in block and BELOW_MINIMUM in block
