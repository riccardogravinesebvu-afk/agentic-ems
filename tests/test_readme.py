import json
from pathlib import Path

from agentic_ems.benchmark.report import END, START, build_block, load_results

ROOT = Path(__file__).parents[1]
README = (ROOT / "README.md").read_text()
OPENING = "Written from scratch on synthetic data. No code, schema or data from any employer or customer."
WHY = (
    "I run a system of this kind in production for seven manufacturers. That code is not mine to publish and "
    "stays where it is. This repository is the method, rebuilt from zero so it can be read, run and criticised: "
    "what happens when the model stops writing SQL and a semantic layer decides how each metric aggregates."
)


def test_the_results_block_is_the_one_the_files_give():
    block = README[README.index(START) + len(START):README.index(END)].strip()
    assert block == build_block(load_results(ROOT / "results"))


def test_the_fixed_sentences_are_there_word_for_word():
    assert README.splitlines()[2] == OPENING
    assert WHY in README.split("## Why this exists")[1]


def test_the_repository_is_not_called_a_copy_of_another_system():
    for phrase in ("a port of", "an extract of", "public version"):
        assert phrase not in README.lower()


def test_the_counts_in_the_prose_are_the_counts_in_the_files():
    runs = load_results(ROOT / "results")["runs"]

    def count(arm: str, **where) -> int:
        return sum(r["arm"] == arm and all(r[key] == value for key, value in where.items()) for r in runs)

    def wrong(arm: str, **where) -> int:
        return sum(count(arm, outcome=outcome, **where) for outcome in ("known_wrong", "other_wrong"))

    assert count("sql") == count("semantic") == count("sql_schema_only") == 275 and wrong("sql") == 0
    assert "no wrong value in 275 runs" in README
    found = README.split("## What I found")[1].split("## What this is")[0]
    assert "made no mistake" in found and "returned 12 wrong values in 275 runs, 4 of them double counts" in found
    assert "refused or returned ingredients" in found
    assert wrong("sql_schema_only") == 12 and "returned 12 wrong values in 275 runs" in README
    assert count("sql_schema_only", category="double_count") == 4 and "4 are double counts" in README
    assert wrong("sql_schema_only", group="metric_definition") == 3 and "3 are energy per unit" in README
    assert wrong("sql_schema_only", group="unanswerable") == 5 and "4 return the solar meter" in README
    assert len({r["question"] for r in runs if r["arm"] == "sql_schema_only" and not r["correct"]}) == 6
    assert "on 6 of the 55 questions" in README and "9 of those 12 runs" in README
    inside = [r for r in runs if r["arm"] == "semantic" and r["group"] not in ("out_of_scope", "unanswerable")]
    assert (sum(r["correct"] for r in inside), len(inside)) == (169, 170) and "169 of 170 runs" in README
    assert count("semantic", group="out_of_scope", outcome="refusal") == 35 and "refused in 35 runs" in README
    assert wrong("semantic") == wrong("semantic", group="out_of_scope") == 30 and "in 30 runs returned" in README
    questions = json.loads((ROOT / "results" / "results.json").read_text())["questions"]
    assert len(questions) == 55 and sum(q["group"] == "out_of_scope" for q in questions.values()) == 13


def test_the_video_script_reads_in_two_minutes():
    assert len((ROOT / "docs" / "video-script.md").read_text().split()) <= 300
