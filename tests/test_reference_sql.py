import tomllib
from pathlib import Path

from agentic_ems.semantic import MetricRequest

BENCHMARK = Path(__file__).resolve().parents[1] / "src" / "agentic_ems" / "benchmark"
REFERENCE = tomllib.loads((BENCHMARK / "reference_sql.toml").read_text())
GROUP = {q["id"]: q["group"] for q in tomllib.loads((BENCHMARK / "questions.toml").read_text())["question"]}
CATEGORIES = {"double_count", "children_sum", "separate_peaks", "extra_rows"}


def test_entries_match_question_groups():
    assert set(REFERENCE) <= set(GROUP)
    for qid, group in GROUP.items():
        entry = REFERENCE.get(qid)
        if group == "unanswerable":
            assert entry is None, qid
            continue
        assert entry is not None and entry["sql"].strip(), qid
        assert ("request" in entry) == (group != "out_of_scope"), qid
        assert bool(entry.get("wrong")) == (group == "hierarchy"), qid


def test_wrong_answers_have_category_and_sql():
    for qid, entry in REFERENCE.items():
        for wrong in entry.get("wrong", []):
            assert wrong["category"] in CATEGORIES, qid
            assert wrong["sql"].strip(), qid


def test_requests_build():
    for entry in REFERENCE.values():
        if "request" in entry:
            MetricRequest(**entry["request"])
