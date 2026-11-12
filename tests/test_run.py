import hashlib
import json
import os
import threading
from typing import Any

import pytest
from fakes import USAGE, ScriptedChatModel, reply
from pydantic import Field

from agentic_ems.agent import MODEL, semantic_arm
from agentic_ems.benchmark import run as run_module
from agentic_ems.benchmark.run import BACKOFF_BASE_S, PRICES, run_benchmark
from agentic_ems.benchmark.sql_arm import sql_arm
from agentic_ems.benchmark.verify import QUESTIONS_FILE, Question, Truth
from agentic_ems.generator import SEED
from agentic_ems.semantic import load_catalog

CACHED = USAGE | {"cache_read_input_tokens": 100}
TRUTH = Truth((["value"], [(1.0,)]), [("double_count", ["value"], [(2.0,)])])


class FlakyModel(ScriptedChatModel):
    failures: list = Field(default_factory=list)  # raised in order before the script plays

    def _generate(self, messages, stop=None, run_manager=None, **kwargs: Any):
        if self.failures:
            raise self.failures.pop(0)
        return super()._generate(messages, stop, run_manager, **kwargs)


class HangingModel(ScriptedChatModel):
    release: Any = None
    hung: bool = False

    def _generate(self, messages, stop=None, run_manager=None, **kwargs: Any):
        if not self.hung:  # the first call hangs past the limit, then fails without touching the script
            self.hung = True
            self.release.wait(10)
            raise RuntimeError("released after the test")
        return super()._generate(messages, stop, run_manager, **kwargs)


def question(qid: str, group: str = "control") -> Question:
    return Question(qid, group, f"Question {qid}?", [], "test", "test")


def refuse(usage: dict = CACHED, model: str = MODEL):
    return reply(("refuse", {"reason": "not in the data"}), usage=usage, model=model)


@pytest.fixture(scope="module")
def arms(con):
    catalog = load_catalog(con)
    return [semantic_arm(con, catalog), sql_arm(con, catalog, "Schema text.")]


def bench(model, questions, arms, tmp_path, **overrides):
    kwargs: dict[str, Any] = {
        "repeats": 1, "budget_eur": 100.0, "eur_per_usd": 0.9, "min_repeats": 1, "first_repeat_eur": 0.0,
        "effort": "high", "run_timeout_s": 30.0, "out_dir": tmp_path, "sleep": lambda seconds: None,
    } | overrides
    return run_benchmark(model, questions, {q.id: TRUTH for q in questions}, arms, **kwargs)


def errors(tmp_path) -> list[dict]:
    return [json.loads(line) for line in (tmp_path / "errors.jsonl").read_text().splitlines()]


def test_runs_are_ordered_by_repeat(arms, tmp_path):
    model = ScriptedChatModel(script=[refuse() for _ in range(8)])
    results = bench(model, [question("Q1"), question("Q2")], arms, tmp_path, repeats=2)
    assert [(r["question"], r["arm"], r["repeat"]) for r in results["runs"]] == [
        (q, arm, repeat) for repeat in (1, 2) for q in ("Q1", "Q2") for arm in ("semantic", "sql")
    ]
    run_dir = tmp_path / "runs" / results["run_id"]
    assert sorted(p.name for p in run_dir.iterdir()) == sorted(
        [f"{q}-{arm}-{repeat}.json" for q in ("Q1", "Q2") for arm in ("semantic", "sql") for repeat in (1, 2)]
        + ["semantic-prompt.json", "sql-prompt.json"]
    )
    transcript = json.loads((tmp_path / results["runs"][0]["transcript"]).read_text())
    assert transcript["record"]["final"] == "refusal" and transcript["score"]["outcome"] == "refusal"
    assert json.loads((tmp_path / "results.json").read_text()) == results
    assert results["failure"] is None


def test_budget_stops_before_a_repeat_that_would_not_fit(arms, tmp_path):
    dollar = USAGE | {"input_tokens": 0, "output_tokens": 50_000, "cache_read_input_tokens": 1}  # just over 1 USD
    model = ScriptedChatModel(script=[refuse(dollar) for _ in range(10)])
    results = bench(model, [question("Q1")], arms, tmp_path / "a", repeats=5, budget_eur=5.0, eur_per_usd=1.0,
                    first_repeat_eur=2.0)
    # Repeat 1 is estimated at 2 EUR, repeat 2 at the mean so far (just over 2), and repeat 3 would pass 5.
    assert [(r["arm"], r["repeat"]) for r in results["runs"]] == [
        ("semantic", 1), ("sql", 1), ("semantic", 2), ("sql", 2)
    ]
    results = bench(model, [question("Q1")], arms, tmp_path / "b", repeats=5, budget_eur=1.0, eur_per_usd=1.0,
                    first_repeat_eur=2.0)
    assert results["runs"] == []


def test_api_error_is_retried_logged_and_not_scored(arms, tmp_path):
    slept: list[float] = []
    model = FlakyModel(failures=[ConnectionError("overloaded")], script=[refuse(), refuse()])
    results = bench(model, [question("Q1")], arms, tmp_path, sleep=slept.append)
    assert [(r["arm"], r["retries"]) for r in results["runs"]] == [("semantic", 1), ("sql", 0)]
    [error] = errors(tmp_path)
    assert error | {"elapsed_s": 0} == {
        "question": "Q1", "arm": "semantic", "repeat": 1, "attempt": 1, "cause": "ConnectionError",
        "message": "overloaded", "elapsed_s": 0,
    }
    assert len(slept) == 1 and 0 <= slept[0] <= BACKOFF_BASE_S


def test_run_over_the_wall_clock_limit_is_a_logged_failure(arms, tmp_path):
    model = HangingModel(release=threading.Event(), script=[refuse(), refuse()])
    try:
        results = bench(model, [question("Q1")], arms, tmp_path, run_timeout_s=0.2)
    finally:
        model.release.set()
    [error] = errors(tmp_path)
    assert error["cause"] == "RunTimeout" and error["arm"] == "semantic"
    assert [(r["arm"], r["outcome"], r["retries"]) for r in results["runs"]] == [
        ("semantic", "refusal", 1), ("sql", "refusal", 0)
    ]


def test_one_model_mismatch_fails_the_attempt(arms, tmp_path):
    model = ScriptedChatModel(script=[refuse(model="claude-other"), refuse(), refuse()])
    results = bench(model, [question("Q1")], arms, tmp_path)
    assert [error["cause"] for error in errors(tmp_path)] == ["ModelMismatch"]
    assert [r["retries"] for r in results["runs"]] == [1, 0] and results["failure"] is None


def test_three_model_mismatches_in_a_row_abort(arms, tmp_path):
    model = ScriptedChatModel(script=[refuse(model="claude-other") for _ in range(3)] + [refuse()])
    results = bench(model, [question("Q1")], arms, tmp_path)
    assert [error["cause"] for error in errors(tmp_path)] == ["ModelMismatch"] * 3
    assert results["runs"] == [] and "model mismatches in a row" in results["failure"]
    assert json.loads((tmp_path / "results.json").read_text())["failure"] == results["failure"]


def test_cost_uses_all_four_token_counts(arms, tmp_path):
    million = dict.fromkeys(USAGE, 1_000_000)
    model = ScriptedChatModel(script=[refuse(million), refuse(million)])
    run = bench(model, [question("Q1")], arms, tmp_path, eur_per_usd=0.9)["runs"][0]
    assert {key: run[key] for key in USAGE} == million
    assert run["cost_eur"] == pytest.approx((4.00 + 5.00 + 0.20 + 20.00) * 0.9)
    assert PRICES == {"input_tokens": 4.00, "cache_creation_input_tokens": 5.00, "cache_read_input_tokens": 0.20,
                      "output_tokens": 20.00}


def test_scored_answer_is_recorded_with_its_sql(arms, tmp_path):
    sql = "SELECT 2.0 AS value"
    model = ScriptedChatModel(script=[refuse(), reply(("run_sql", {"sql": sql}), usage=CACHED),
                                      reply(text="Done.", usage=CACHED)])
    results = bench(model, [question("Q1", "hierarchy")], arms, tmp_path)
    run = results["runs"][1]
    assert (run["arm"], run["group"], run["outcome"], run["category"], run["correct"]) == (
        "sql", "hierarchy", "known_wrong", "double_count", False
    )
    assert run["sql"] == sql and run["answer"] == {"columns": ["value"], "rows": [[2.0]]}
    assert results["questions"]["Q1"] == {
        "group": "hierarchy", "text": "Question Q1?", "reference": {"columns": ["value"], "rows": [[1.0]]},
        "correct_runs": {"semantic": 0, "sql": 0},
    }


def test_versions_are_recorded(arms, tmp_path):
    results = bench(ScriptedChatModel(script=[refuse(), refuse()]), [question("Q1")], arms, tmp_path,
                    effort="medium")
    versions = results["versions"]
    assert versions["seed"] == SEED and versions["model"] == MODEL and versions["effort"] == "medium"
    assert versions["questions_sha256"] == hashlib.sha256(QUESTIONS_FILE.read_bytes()).hexdigest()
    assert len(versions["scorer_sha256"]) == 64 and set(versions["prompts_sha256"]) == {"semantic", "sql"}
    prompt = (tmp_path / "runs" / results["run_id"] / "sql-prompt.json").read_text()
    assert "Schema text." in prompt
    assert {key: results[key] for key in ("eur_per_usd", "min_repeats", "first_repeat_eur")} == {
        "eur_per_usd": 0.9, "min_repeats": 1, "first_repeat_eur": 0.0
    }
    assert results["prices"] == {"usd_per_million_tokens": PRICES}


def test_run_fails_when_neither_arm_reads_from_cache(arms, tmp_path):
    results = bench(ScriptedChatModel(script=[refuse(USAGE), refuse(USAGE)]), [question("Q1")], arms, tmp_path)
    assert results["failure"] == "neither arm ever read from cache"
    assert json.loads((tmp_path / "results.json").read_text())["failure"] == results["failure"]
    assert len(results["runs"]) == 2  # the evidence is kept


def test_main_passes_the_arguments_and_turns_langsmith_off(monkeypatch, db_path, tmp_path):
    seen: dict[str, Any] = {}

    def fake_benchmark(model, questions, truths, arms, **kwargs):
        seen.update(kwargs, model=model, questions=[q.id for q in questions], arms=[a.name for a in arms],
                    tracing=os.environ.get("LANGSMITH_TRACING"))
        return {"failure": None, "runs": []}

    monkeypatch.delenv("LANGSMITH_TRACING", raising=False)
    monkeypatch.setattr(run_module, "DEFAULT_DB", db_path)
    monkeypatch.setattr(run_module, "RESULTS", tmp_path)
    monkeypatch.setattr(run_module, "make_model", lambda **kwargs: kwargs)
    monkeypatch.setattr(run_module, "run_benchmark", fake_benchmark)
    argv = ["--repeats", "3", "--min-repeats", "2", "--budget-eur", "12.5", "--first-repeat-eur", "1.5",
            "--eur-per-usd", "0.92", "--effort", "high", "--max-tokens", "4000", "--run-timeout-s", "90",
            "--arm", "sql", "--only", "H1", "C1"]
    assert run_module.main(argv) == 0
    assert seen["tracing"] == "false" and seen["model"] == {"effort": "high", "max_tokens": 4000}
    assert seen["questions"] == ["H1", "C1"] and seen["arms"] == ["sql"]
    assert {key: seen[key] for key in ("repeats", "min_repeats", "budget_eur", "first_repeat_eur", "eur_per_usd",
                                       "effort", "run_timeout_s", "out_dir")} == {
        "repeats": 3, "min_repeats": 2, "budget_eur": 12.5, "first_repeat_eur": 1.5, "eur_per_usd": 0.92,
        "effort": "high", "run_timeout_s": 90.0, "out_dir": tmp_path,
    }
    with pytest.raises(SystemExit):
        run_module.main(argv[:-7])  # no --run-timeout-s
