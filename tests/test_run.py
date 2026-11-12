import hashlib
import json
import os
import threading
import time
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import anthropic
import httpx2
import pytest
from fakes import USAGE, ScriptedChatModel, reply
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field

from agentic_ems.agent import MODEL, semantic_arm
from agentic_ems.benchmark import run as run_module
from agentic_ems.benchmark.report import build_block
from agentic_ems.benchmark.run import (
    BACKOFF_BASE_S,
    IN_PROGRESS,
    PRICES,
    cost_eur,
    run_benchmark,
)
from agentic_ems.benchmark.sql_arm import sql_arm
from agentic_ems.benchmark.verify import QUESTIONS_FILE, Question, Truth
from agentic_ems.semantic import load_catalog

CACHED = USAGE | {"cache_read_input_tokens": 100}
DOLLAR = USAGE | {"input_tokens": 0, "output_tokens": 50_000, "cache_read_input_tokens": 1}  # just over 1 USD
TRUTH = Truth((["value"], [(1.0,)]), [("double_count", ["value"], [(2.0,)])])
REQUEST = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")  # never sent


def until(done: Callable[[], bool], limit_s: float = 10.0) -> None:
    """Polls on the monotonic clock: on this Python build timed lock waits end on the wall clock, which the host steps."""
    deadline = time.monotonic() + limit_s
    while not done():
        assert time.monotonic() < deadline, f"not done within {limit_s:g} seconds"
        time.sleep(0.01)


def connection_error() -> anthropic.APIConnectionError:
    return anthropic.APIConnectionError(request=REQUEST)


class FlakyModel(ScriptedChatModel):
    """Raises the exception keyed by the number of the call (1-based) instead of replying; can run a probe first."""

    failures: dict = Field(default_factory=dict)
    probe: Any = None
    calls: int = 0

    def _generate(self, messages, stop=None, run_manager=None, **kwargs: Any):
        self.calls += 1
        if self.probe:
            self.probe(self.calls)
        if self.calls in self.failures:
            raise self.failures[self.calls]
        return super()._generate(messages, stop, run_manager, **kwargs)


class HangingModel(FlakyModel):
    """The first call hangs until released, then answers with `late`, a tool call, so the loop would call again."""

    release: Any = None
    late: Any = None
    hung: Any = None  # the thread of the hung call, once it is waiting

    def _generate(self, messages, stop=None, run_manager=None, **kwargs: Any):
        if self.calls == 0:
            self.calls += 1
            self.hung = threading.current_thread()
            until(self.release.is_set)
            return ChatResult(generations=[ChatGeneration(message=self.late)])
        return super()._generate(messages, stop, run_manager, **kwargs)

    def wait(self, thread: threading.Thread, limit_s: float) -> None:
        """The harness's wait, driven by events: only the hung call, while not released, is past its limit."""
        while thread.is_alive():
            if thread is self.hung and not self.release.is_set():
                return
            thread.join(0.01)


def question(qid: str, group: str = "control") -> Question:
    return Question(qid, group, f"Question {qid}?", [], "test", "test")


def refuse(usage: dict = CACHED, model: str = MODEL):
    return reply(("refuse", {"reason": "not in the data"}), usage=usage, model=model)


@pytest.fixture(scope="module")
def arms(con):
    catalog = load_catalog(con)
    return [semantic_arm(con, catalog), sql_arm(con, "Schema text.")]


@pytest.fixture(scope="module")
def seed(con):
    with con.cursor() as cursor:
        return cursor.execute("SELECT seed FROM dataset_info").fetchone()[0]


@pytest.fixture
def bench(arms, seed, tmp_path):
    def run(model, questions, **overrides):
        kwargs: dict[str, Any] = {
            "repeats": 1, "budget_eur": 100.0, "eur_per_usd": 0.9, "min_repeats": 1, "first_repeat_eur": 0.0,
            "effort": "high", "seed": seed, "run_timeout_s": 30.0, "max_attempts": 5, "out_dir": tmp_path,
            "sleep": lambda seconds: None,
        } | overrides
        return run_benchmark(model, questions, {q.id: TRUTH for q in questions}, arms, **kwargs)
    return run


def errors(tmp_path) -> list[dict]:
    return [json.loads(line) for line in (tmp_path / "errors.jsonl").read_text().splitlines()]


def saved(tmp_path) -> dict:
    return json.loads((tmp_path / "results.json").read_text())


def test_runs_are_ordered_by_repeat(bench, tmp_path):
    results = bench(ScriptedChatModel(script=[refuse() for _ in range(8)]), [question("Q1"), question("Q2")],
                    repeats=2)
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
    assert saved(tmp_path) == results
    assert results["failure"] is None and results["stopped"] is None and results["unscored_eur"] == 0


def test_budget_is_checked_before_every_repeat(bench, tmp_path):
    model = ScriptedChatModel(script=[refuse(DOLLAR) for _ in range(10)])
    # Each run costs just over 1 EUR, so a repeat of 4 runs just over 4: repeat 2 would pass 6.5.
    results = bench(model, [question("Q1"), question("Q2")], repeats=3, budget_eur=6.5, eur_per_usd=1.0,
                    first_repeat_eur=4.0)
    assert len(results["runs"]) == 4 and "one more repeat" in results["stopped"] and results["failure"] is None
    block = build_block(results)
    assert "1 of 3 repeats completed" in block and "semantic 0 of 2 runs, sql 0 of 2 runs" in block


def test_budget_is_checked_before_every_run(bench, tmp_path):
    model = ScriptedChatModel(script=[refuse(DOLLAR) for _ in range(10)])
    # The first repeat is estimated at 2 EUR (0.5 a run) and fits; its runs cost 1 EUR each, so run 4 would pass 3.5.
    results = bench(model, [question("Q1"), question("Q2")], repeats=3, budget_eur=3.5, eur_per_usd=1.0,
                    first_repeat_eur=2.0)
    assert len(results["runs"]) == 3 and "the next run" in results["stopped"] and results["failure"] is None
    # The stop fell inside repeat 1: no repeat is published, and the counts stay equal per arm.
    block = build_block(results)
    assert "0 of 3 repeats completed" in block and "semantic 0 of 0 runs, sql 0 of 0 runs" in block


def test_the_run_estimate_includes_failed_attempts(bench, tmp_path):
    # Run 1 costs 1 EUR scored plus 1 EUR in a failed attempt: 2 EUR in all, so run 3 (at 3 EUR spent) would pass 4.5.
    script = [reply(("run_sql", {"sql": "SELECT 1.0 AS value"}), usage=DOLLAR)] + [refuse(DOLLAR) for _ in range(5)]
    model = FlakyModel(failures={2: connection_error()}, script=script)
    results = bench(model, [question("Q1"), question("Q2")], budget_eur=4.5, eur_per_usd=1.0, first_repeat_eur=0.0)
    assert len(results["runs"]) == 2 and "the next run may cost 2.00" in results["stopped"]


def test_a_stop_before_any_run_gives_the_budget_reason(bench, tmp_path):
    results = bench(ScriptedChatModel(script=[]), [question("Q1")], budget_eur=1.0, first_repeat_eur=4.0)
    assert results["runs"] == [] and "budget" in results["stopped"] and results["failure"] is None


def test_api_error_is_retried_logged_and_not_scored(bench, tmp_path):
    (tmp_path / "errors.jsonl").write_text('{"earlier": "line"}\n')
    slept: list[float] = []
    model = FlakyModel(failures={1: connection_error()}, script=[refuse(), refuse()])
    results = bench(model, [question("Q1")], sleep=slept.append)
    assert [(r["arm"], r["retries"]) for r in results["runs"]] == [("semantic", 1), ("sql", 0)]
    earlier, error = errors(tmp_path)
    assert earlier == {"earlier": "line"}  # appended, not overwritten
    assert error | {"elapsed_s": 0} == {
        "run_id": results["run_id"], "question": "Q1", "arm": "semantic", "repeat": 1, "attempt": 1,
        "cause": "APIConnectionError", "message": "Connection error.", "elapsed_s": 0,
    }
    assert len(slept) == 1 and 0 <= slept[0] <= BACKOFF_BASE_S


@pytest.mark.parametrize("failure", [
    anthropic.BadRequestError("bad", response=httpx2.Response(400, request=REQUEST), body=None),
    ValueError("a harness bug"),
])
def test_other_errors_end_the_benchmark_with_results_kept(bench, tmp_path, failure):
    model = FlakyModel(failures={2: failure}, script=[refuse(), refuse()])
    with pytest.raises(type(failure)):
        bench(model, [question("Q1")])
    kept = saved(tmp_path)
    assert len(kept["runs"]) == 1 and kept["failure"].startswith(type(failure).__name__)


def test_an_attempt_that_fails_after_a_billed_response_is_priced(bench, tmp_path):
    script = [reply(("run_sql", {"sql": "SELECT 1.0 AS value"}), usage=DOLLAR), refuse(DOLLAR), refuse(DOLLAR)]
    model = FlakyModel(failures={2: connection_error()}, script=script)
    results = bench(model, [question("Q1")], eur_per_usd=1.0)
    one = cost_eur(DOLLAR, 1.0)
    assert results["unscored_eur"] == pytest.approx(one)
    assert [r["cost_eur"] for r in results["runs"]] == pytest.approx([one, one])


def test_run_over_the_wall_clock_limit_makes_no_new_request(bench, tmp_path):
    model = HangingModel(release=threading.Event(), script=[refuse(), refuse()],
                         late=reply(("run_sql", {"sql": "SELECT 1.0 AS value"}), usage=CACHED))
    try:
        results = bench(model, [question("Q1")], wait=model.wait)
        files = [(tmp_path / name).read_text() for name in ("results.json", "errors.jsonl")]
        calls = model.calls
    finally:
        model.release.set()
    # Released, the attempt ends: the meter refuses its next call.
    until(lambda: all(thread.name != "benchmark-attempt" for thread in threading.enumerate()))
    [error] = errors(tmp_path)
    assert error["cause"] == "RunTimeout" and error["arm"] == "semantic"
    assert [(r["arm"], r["outcome"], r["retries"]) for r in results["runs"]] == [
        ("semantic", "refusal", 1), ("sql", "refusal", 0)
    ]
    assert results["attempts_still_running"] == 1  # still hung after the final wait
    # Released, the abandoned run ran its query but made no new model call, and nothing published changed.
    assert model.calls == calls and len(model.script) == 0
    assert [(tmp_path / name).read_text() for name in ("results.json", "errors.jsonl")] == files


def test_a_late_response_of_an_abandoned_run_is_priced_into_the_file(bench, tmp_path):
    late = reply(("run_sql", {"sql": "SELECT 1.0 AS value"}), usage=DOLLAR)
    model = HangingModel(release=threading.Event(), late=late, script=[refuse(), refuse()])
    model.probe = lambda call: call == 3 and model.release.set()  # released during the last run
    results = bench(model, [question("Q1")], eur_per_usd=1.0, wait=model.wait)
    assert results["attempts_still_running"] == 0 and model.calls == 3
    assert results["unscored_eur"] == pytest.approx(cost_eur(DOLLAR, 1.0))
    assert saved(tmp_path)["unscored_eur"] == results["unscored_eur"]


def test_an_interrupt_prices_the_attempt_and_names_the_failure(bench, tmp_path, monkeypatch):
    def interrupted(limit_s, fn, meter, wait):
        fn()  # the attempt's response is billed and priced, then Ctrl-C arrives
        raise KeyboardInterrupt

    monkeypatch.setattr(run_module, "_within", interrupted)
    with pytest.raises(KeyboardInterrupt):
        bench(ScriptedChatModel(script=[refuse(DOLLAR)]), [question("Q1")], eur_per_usd=1.0)
    kept = saved(tmp_path)
    assert kept["failure"] == "KeyboardInterrupt" and kept["unscored_eur"] == pytest.approx(cost_eur(DOLLAR, 1.0))


def test_a_second_interrupt_in_the_final_wait_keeps_the_save_and_the_error(bench, tmp_path, monkeypatch):
    class Stubborn:  # an abandoned attempt's thread that never ends
        def is_alive(self):
            return True

    def second_ctrl_c(thread, limit_s):
        raise KeyboardInterrupt

    calls = []

    def within(limit_s, fn, meter, wait):
        calls.append(meter)
        if len(calls) == 1:
            meter.thread = Stubborn()  # type: ignore[assignment]
            raise run_module.RunTimeout("abandoned")
        raise ValueError("a harness bug")

    monkeypatch.setattr(run_module, "_within", within)
    with pytest.raises(ValueError):
        bench(ScriptedChatModel(script=[]), [question("Q1")], wait=second_ctrl_c)
    kept = saved(tmp_path)
    assert kept["failure"] == "ValueError: a harness bug" and kept["attempts_still_running"] == 1


def test_the_run_limit_is_measured_on_the_monotonic_clock(monkeypatch):
    # Timed lock waits in this CPython build follow the wall clock; the harness's wait must not.
    release = threading.Event()
    thread = threading.Thread(target=release.wait, daemon=True)
    thread.start()
    ticks = iter(range(100))
    # Only the clock the harness sees: the test's own waits keep the real one.
    monkeypatch.setattr(run_module, "time", SimpleNamespace(monotonic=lambda: float(next(ticks)), sleep=lambda s: None))
    try:
        run_module._wait(thread, 3.0)  # returns after three ticks of the monotonic clock, alive
        assert thread.is_alive() and next(ticks) == 4
    finally:
        release.set()
        until(lambda: not thread.is_alive())
    run_module._wait(thread, 3.0)  # an ended thread returns at once
    assert next(ticks) == 6


def test_one_model_mismatch_fails_the_attempt_and_is_priced_in_full(bench, tmp_path):
    model = ScriptedChatModel(script=[refuse(DOLLAR, model="claude-other"), refuse(), refuse()])
    results = bench(model, [question("Q1")], eur_per_usd=1.0)
    assert [error["cause"] for error in errors(tmp_path)] == ["ModelMismatch"]
    assert [r["retries"] for r in results["runs"]] == [1, 0] and results["failure"] is None
    assert results["unscored_eur"] == pytest.approx(cost_eur(DOLLAR, 1.0))


def test_three_model_mismatches_in_a_row_abort(bench, tmp_path):
    model = ScriptedChatModel(script=[refuse(model="claude-other") for _ in range(3)] + [refuse()])
    results = bench(model, [question("Q1")])
    assert [error["cause"] for error in errors(tmp_path)] == ["ModelMismatch"] * 3
    assert results["runs"] == [] and "model mismatches in a row" in results["failure"]
    assert saved(tmp_path)["failure"] == results["failure"]


def test_attempts_are_capped(bench, tmp_path):
    model = FlakyModel(failures={n: connection_error() for n in (1, 2)}, script=[refuse()])
    results = bench(model, [question("Q1")], max_attempts=2)
    assert results["runs"] == [] and "failed 2 attempts" in results["failure"]


def test_results_are_saved_after_every_run(bench, tmp_path):
    seen: list[tuple] = []

    def probe(call: int) -> None:
        kept = saved(tmp_path)
        seen.append((call, len(kept["runs"]), kept["failure"]))

    bench(FlakyModel(probe=probe, script=[refuse(), refuse()]), [question("Q1")])
    assert seen == [(1, 0, IN_PROGRESS), (2, 1, IN_PROGRESS)]


def test_cost_uses_all_four_token_counts(bench, tmp_path):
    million = dict.fromkeys(USAGE, 1_000_000)
    run = bench(ScriptedChatModel(script=[refuse(million), refuse(million)]), [question("Q1")],
                eur_per_usd=0.9)["runs"][0]
    assert {key: run[key] for key in USAGE} == million
    assert run["cost_eur"] == pytest.approx((4.00 + 5.00 + 0.20 + 20.00) * 0.9)
    assert PRICES == {"input_tokens": 4.00, "cache_creation_input_tokens": 5.00, "cache_read_input_tokens": 0.20,
                      "output_tokens": 20.00}


def test_scored_answer_is_recorded_with_its_sql(bench, tmp_path):
    sql = "SELECT 2.0 AS value"
    model = ScriptedChatModel(script=[refuse(), reply(("run_sql", {"sql": sql}), usage=CACHED),
                                      reply(text="Done.", usage=CACHED)])
    results = bench(model, [question("Q1", "hierarchy")])
    run = results["runs"][1]
    assert (run["arm"], run["group"], run["outcome"], run["category"], run["correct"]) == (
        "sql", "hierarchy", "known_wrong", "double_count", False
    )
    assert run["sql"] == sql and run["answer"] == {"columns": ["value"], "rows": [[2.0]]}
    assert run["cost_eur"] == pytest.approx(2 * cost_eur(CACHED, 0.9))  # both responses of the run
    assert results["questions"]["Q1"] == {
        "group": "hierarchy", "text": "Question Q1?", "reference": {"columns": ["value"], "rows": [[1.0]]},
    }


def test_versions_are_recorded(bench, seed, tmp_path):
    results = bench(ScriptedChatModel(script=[refuse(), refuse()]), [question("Q1")], effort="medium")
    versions = results["versions"]
    assert versions["seed"] == seed and versions["model"] == MODEL and versions["effort"] == "medium"
    assert versions["questions_sha256"] == hashlib.sha256(QUESTIONS_FILE.read_bytes()).hexdigest()
    assert len(versions["scorer_sha256"]) == 64 and set(versions["prompts_sha256"]) == {"semantic", "sql"}
    prompt = (tmp_path / "runs" / results["run_id"] / "sql-prompt.json").read_text()
    assert "Schema text." in prompt
    assert {key: results[key] for key in ("eur_per_usd", "min_repeats", "first_repeat_eur")} == {
        "eur_per_usd": 0.9, "min_repeats": 1, "first_repeat_eur": 0.0
    }
    assert results["prices"] == {"usd_per_million_tokens": PRICES}


def test_run_fails_when_neither_arm_reads_from_cache(bench, tmp_path):
    results = bench(ScriptedChatModel(script=[refuse(USAGE), refuse(USAGE)]), [question("Q1")])
    assert results["failure"] == "no arm ever read from cache"
    assert saved(tmp_path)["failure"] == results["failure"]
    assert len(results["runs"]) == 2  # the evidence is kept


ARGV = ["--repeats", "3", "--min-repeats", "2", "--budget-eur", "12.5", "--first-repeat-eur", "1.5",
        "--eur-per-usd", "0.92", "--effort", "high", "--max-tokens", "4000", "--max-attempts", "4",
        "--run-timeout-s", "90", "--arm", "sql", "--only", "H1", "C1"]


@pytest.fixture
def fake_main(monkeypatch, db_path, tmp_path):
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
    return seen


def test_main_passes_the_arguments_and_turns_langsmith_off(fake_main, seed, tmp_path):
    assert run_module.main(ARGV) == 0
    assert fake_main["tracing"] == "false" and fake_main["model"] == {"effort": "high", "max_tokens": 4000}
    assert fake_main["questions"] == ["H1", "C1"] and fake_main["arms"] == ["sql"]
    keys = ("repeats", "min_repeats", "budget_eur", "first_repeat_eur", "eur_per_usd", "effort", "seed",
            "run_timeout_s", "max_attempts", "out_dir")
    assert {key: fake_main[key] for key in keys} == {
        "repeats": 3, "min_repeats": 2, "budget_eur": 12.5, "first_repeat_eur": 1.5, "eur_per_usd": 0.92,
        "effort": "high", "seed": seed, "run_timeout_s": 90.0, "max_attempts": 4, "out_dir": tmp_path,
    }


def test_main_accepts_a_pilot_below_the_minimum(fake_main):
    argv = list(ARGV)
    argv[argv.index("--repeats") + 1] = "1"
    assert run_module.main(argv) == 0 and (fake_main["repeats"], fake_main["min_repeats"]) == (1, 2)


@pytest.mark.parametrize("change", [
    {"--run-timeout-s": None}, {"--max-attempts": None}, {"--min-repeats": "0"}, {"--max-attempts": "0"},
    {"--first-repeat-eur": "0"},
])
def test_main_rejects_missing_or_inconsistent_arguments(fake_main, change):
    argv = list(ARGV)
    for flag, value in change.items():
        at = argv.index(flag)
        argv[at:at + 2] = [] if value is None else [flag, value]
    with pytest.raises(SystemExit):
        run_module.main(argv)
    assert "repeats" not in fake_main


def test_schema_only_arm_runs_alone_into_its_own_folder(fake_main, tmp_path):
    argv = [*ARGV[:ARGV.index("--arm")], "--arm", "sql_schema_only", "--out-dir", str(tmp_path / "schema_only")]
    assert run_module.main(argv) == 0
    assert fake_main["arms"] == ["sql_schema_only"] and fake_main["out_dir"] == tmp_path / "schema_only"


def test_main_never_overwrites_a_recorded_run(fake_main, tmp_path):
    (tmp_path / "results.json").write_text("{}")
    with pytest.raises(SystemExit):
        run_module.main(ARGV)
    assert "repeats" not in fake_main
