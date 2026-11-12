"""Run the benchmark: every question in every arm, repeat by repeat, while the budget allows.

Run with: uv run python -m agentic_ems.benchmark.run --help
Writes results/results.json, results/errors.jsonl and one transcript per run under results/runs/<run_id>/.
"""

import argparse
import functools
import hashlib
import itertools
import json
import os
import random
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import anthropic
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.outputs import ChatGeneration, LLMResult

from agentic_ems.agent import (
    MODEL,
    USAGE_KEYS,
    Arm,
    ModelMismatch,
    RunRecord,
    make_model,
    run_agent,
    semantic_arm,
    tracing_callbacks,
)
from agentic_ems.benchmark import scorer
from agentic_ems.benchmark.sql_arm import SCHEMA_ONLY, schema_only_md, sql_arm
from agentic_ems.benchmark.verify import (
    QUESTIONS_FILE,
    Question,
    Truth,
    load_questions,
    load_truth,
)
from agentic_ems.semantic import DEFAULT_DB, load_catalog, open_db

ROOT = Path(__file__).resolve().parents[3]
RESULTS = ROOT / "results"
# claude-opus-5-5 in USD per million tokens, from the claude-api skill (models table cached 2026-11-03).
# A cache write is 1.25 times the input price: the arms use the default 5-minute cache.
PRICES = {"input_tokens": 4.00, "cache_creation_input_tokens": 5.00, "cache_read_input_tokens": 0.20, "output_tokens": 20.00}
MAX_MISMATCHES = 3
BACKOFF_BASE_S, BACKOFF_CAP_S = 2.0, 60.0
POLL_S = 0.01
IN_PROGRESS = "in progress: the benchmark has not finished"


class RunTimeout(RuntimeError):
    """A run went past its wall-clock limit."""


class Abort(RuntimeError):
    """The benchmark cannot go on; the message says why."""


# Harness failures that are retried: connection and client timeout, 408, 429, every 5xx (529 overloaded
# included), the wall-clock limit and a response from another model. Anything else ends the benchmark.
RETRIED = (
    anthropic.APIConnectionError, anthropic.RequestTimeoutError, anthropic.RateLimitError,
    anthropic.InternalServerError, RunTimeout, ModelMismatch,
)


def cost_eur(usage: dict, eur_per_usd: float) -> float:
    return sum((usage.get(key) or 0) * price for key, price in PRICES.items()) / 1_000_000 * eur_per_usd


class Meter(BaseCallbackHandler):
    """One attempt's spend, priced response by response; once abandoned, the attempt sends no new request."""

    raise_error = True  # so the error raised below stops the run instead of being logged and skipped

    def __init__(self, eur_per_usd: float) -> None:
        self.eur_per_usd = eur_per_usd
        self.cost = 0.0  # written only by the attempt's thread
        self.abandoned = False
        self.thread: threading.Thread | None = None

    def on_chat_model_start(self, serialized: dict, messages: list, **kwargs) -> None:
        if self.abandoned:
            raise RunTimeout("the attempt was abandoned at its wall-clock limit")

    def on_llm_end(self, response: LLMResult, **kwargs) -> None:
        # The raw usage of each response, the field agent.py sums for the run's token counts.
        for generation in itertools.chain.from_iterable(response.generations):
            if isinstance(generation, ChatGeneration):
                raw = generation.message.response_metadata.get("usage") or {}
                self.cost += cost_eur({key: raw.get(key) for key in USAGE_KEYS}, self.eur_per_usd)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _prompt(arm: Arm) -> dict:
    return {"system": arm.system, "tools": arm.tools}


def _plain(value):
    """The JSON form of values DuckDB rows hold."""
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    return str(value)


def _dump(path: Path, data) -> None:
    partial = path.with_name(path.name + ".tmp")  # a death mid-write leaves the previous file whole
    partial.write_text(json.dumps(data, indent=1, default=_plain) + "\n")
    partial.replace(path)


def _wait(thread: threading.Thread, limit_s: float) -> None:
    """Until the thread ends or limit_s passes on the monotonic clock.

    Not thread.join(limit_s): where CPython lacks sem_clockwait (this uv-managed 3.14 build), a timed lock
    wait ends at a wall-clock deadline, so a step of the system clock would shorten or stretch the limit.
    time.sleep runs on the monotonic clock.
    """
    deadline = time.monotonic() + limit_s
    while thread.is_alive() and deadline - time.monotonic() > 0:
        time.sleep(POLL_S)


Wait = Callable[[threading.Thread, float], None]


def _within(limit_s: float, fn: Callable[[], RunRecord], meter: Meter, wait: Wait) -> RunRecord:
    # A daemon thread, because a run past its limit cannot be stopped: it is abandoned and never blocks exit.
    out: dict = {}

    def target() -> None:
        try:
            out["record"] = fn()
        except Exception as exc:  # noqa: BLE001 - re-raised in the calling thread below
            out["error"] = exc

    thread = meter.thread = threading.Thread(target=target, name="benchmark-attempt", daemon=True)
    thread.start()
    wait(thread, limit_s)
    if thread.is_alive():
        meter.abandoned = True
        raise RunTimeout(f"the run took longer than {limit_s:g} s")
    if "error" in out:
        raise out["error"]
    return out["record"]


def run_benchmark(model, questions: list[Question], truths: dict[str, Truth], arms: list[Arm], *, repeats: int,
                  budget_eur: float, eur_per_usd: float, min_repeats: int, first_repeat_eur: float, effort: str,
                  seed: int, run_timeout_s: float, max_attempts: int, out_dir: Path, callbacks: list | None = None,
                  sleep: Callable[[float], None] = time.sleep, wait: Wait = _wait) -> dict:
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    run_dir = out_dir / "runs" / run_id
    run_dir.mkdir(parents=True)
    for arm in arms:
        _dump(run_dir / f"{arm.name}-prompt.json", _prompt(arm))
    results: dict = {
        "run_id": run_id,
        "versions": {
            "seed": seed,
            "questions_sha256": _sha256(QUESTIONS_FILE.read_bytes()),
            "scorer_sha256": _sha256(Path(scorer.__file__).read_bytes()),
            "prompts_sha256": {arm.name: _sha256(json.dumps(_prompt(arm), sort_keys=True).encode()) for arm in arms},
            "model": MODEL,
            "effort": effort,
        },
        "eur_per_usd": eur_per_usd,
        "prices": {"usd_per_million_tokens": PRICES},
        "budget_eur": budget_eur,
        "repeats": repeats,
        "min_repeats": min_repeats,
        "first_repeat_eur": first_repeat_eur,
        "questions": {
            q.id: {
                "group": q.group,
                "text": q.text,
                "reference": None if (ref := truths[q.id].reference) is None else {"columns": ref[0], "rows": ref[1]},
            }
            for q in questions
        },
        "runs": [],
        "unscored_eur": 0.0,  # spent on attempts that produced no scored run
        "stopped": None,
        "attempts_still_running": 0,  # abandoned attempts still alive after the final wait
        "failure": IN_PROGRESS,
    }
    unscored: list[Meter] = []  # a meter of an abandoned attempt can still grow when its late response arrives
    run_meters: list[list[Meter]] = []  # per scored run, the meters of all its attempts

    def save() -> None:
        results["unscored_eur"] = sum(meter.cost for meter in unscored)
        _dump(out_dir / "results.json", results)

    def spent() -> float:
        return sum(run["cost_eur"] for run in results["runs"]) + sum(meter.cost for meter in unscored)

    errors = (out_dir / "errors.jsonl").open("a")
    mismatches = 0

    def attempt(q: Question, arm: Arm, repeat: int) -> tuple[RunRecord, int, list[Meter]]:
        """The run's record, retry count and the meters of its attempts, the scored one last.

        Harness failures are logged and retried, never scored.
        """
        nonlocal mismatches
        meters: list[Meter] = []
        for retries in range(max_attempts):
            meter = Meter(eur_per_usd)
            meters.append(meter)
            started = time.monotonic()
            try:
                record = _within(run_timeout_s, functools.partial(
                    run_agent, model, arm, q.text, callbacks=[*(callbacks or []), meter]), meter, wait)
            except RETRIED as exc:
                unscored.append(meter)
                mismatches = mismatches + 1 if isinstance(exc, ModelMismatch) else 0
                errors.write(json.dumps({
                    "run_id": run_id, "question": q.id, "arm": arm.name, "repeat": repeat, "attempt": retries + 1,
                    "cause": type(exc).__name__, "message": str(exc),
                    "elapsed_s": round(time.monotonic() - started, 3),
                }) + "\n")
                errors.flush()
                save()
                if mismatches == MAX_MISMATCHES:
                    raise Abort(f"{MAX_MISMATCHES} model mismatches in a row, the last: {exc}") from exc
                if retries + 1 < max_attempts:
                    sleep(random.uniform(0, min(BACKOFF_CAP_S, BACKOFF_BASE_S * 2**retries)))  # full jitter
                continue
            except BaseException:  # an error that ends the benchmark, or an interrupt such as Ctrl-C
                meter.abandoned = True
                unscored.append(meter)
                raise
            mismatches = 0
            return record, retries, meters
        raise Abort(f"{q.id} in the {arm.name} arm, repeat {repeat}, failed {max_attempts} attempts")

    failure: str | None = IN_PROGRESS
    first_run_eur = first_repeat_eur / max(1, len(questions) * len(arms))
    try:
        save()
        for repeat, q, arm in itertools.product(range(1, repeats + 1), questions, arms):
            if q is questions[0] and arm is arms[0]:
                # One more repeat must fit: estimated by the first repeat's figure, then the mean spend per repeat.
                estimate = first_repeat_eur if repeat == 1 else spent() / (repeat - 1)
                if spent() + estimate > budget_eur:
                    results["stopped"] = (f"the budget: {spent():.2f} EUR spent and one more repeat may cost "
                                          f"{estimate:.2f} EUR, over the {budget_eur:.2f} EUR budget")
                    break
            # The hard stop: the costliest run so far, failed attempts included, must fit.
            largest = max((sum(m.cost for m in ms) for ms in run_meters), default=first_run_eur)
            if spent() + largest > budget_eur:
                results["stopped"] = (f"the budget: {spent():.2f} EUR spent and the next run may cost {largest:.2f} "
                                      f"EUR, over the {budget_eur:.2f} EUR budget")
                break
            record, retries, meters = attempt(q, arm, repeat)
            run_meters.append(meters)
            meter = meters[-1]
            result = scorer.score(q, truths[q.id], record)
            name = f"{q.id}-{arm.name}-{repeat}.json"
            _dump(run_dir / name, {
                "question": asdict(q), "truth": asdict(truths[q.id]), "record": asdict(record),
                "score": asdict(result), "cost_eur": meter.cost, "retries": retries,
            })
            rows = record.final == "rows"
            results["runs"].append({
                "question": q.id, "group": q.group, "arm": arm.name, "repeat": repeat,
                "outcome": result.outcome, "category": result.category, "correct": result.correct,
                "earlier_correct": result.earlier_correct, **record.usage, "cost_eur": meter.cost,
                "retries": retries,
                "sql": next((c["sql"] for c in reversed(record.calls) if c["ok"] and c["sql"]), None) if rows else None,
                "answer": {"columns": record.columns, "rows": record.rows} if rows else None,
                "transcript": f"runs/{run_id}/{name}",
            })
            save()
        failure = None
    except Abort as exc:
        failure = str(exc)
    except BaseException as exc:
        failure = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
        raise
    finally:
        errors.close()
        # Wait a bounded time for abandoned attempts, so a late billed response is priced into the file.
        deadline = time.monotonic() + run_timeout_s
        try:
            for meter in unscored:
                if meter.thread is not None:
                    wait(meter.thread, max(0.0, deadline - time.monotonic()))
        except KeyboardInterrupt:
            pass  # a second Ctrl-C ends the wait only: the save below still runs and the first error stays
        results["attempts_still_running"] = sum(m.thread is not None and m.thread.is_alive() for m in unscored)
        if failure is None and results["runs"] and not any(run["cache_read_input_tokens"] for run in results["runs"]):
            failure = "neither arm ever read from cache"
        results["failure"] = failure
        save()
    return json.loads(json.dumps(results, default=_plain))  # the same plain values as the file


def main(argv: list[str] | None = None) -> int:
    # Before any LangChain call: no transcript leaves the machine through LangSmith.
    os.environ["LANGSMITH_TRACING"] = "false"
    os.environ["LANGCHAIN_TRACING_V2"] = "false"  # the legacy name of the same switch
    parser = argparse.ArgumentParser(description="Run the benchmark and write results/.")
    parser.add_argument("--repeats", type=int, required=True)
    parser.add_argument("--min-repeats", type=int, required=True, help="below this many completed repeats no comparison is claimed")
    parser.add_argument("--budget-eur", type=float, required=True)
    parser.add_argument("--first-repeat-eur", type=float, required=True, help="the estimated cost of the first repeat")
    parser.add_argument("--eur-per-usd", type=float, required=True)
    parser.add_argument("--effort", required=True, choices=["low", "medium", "high", "xhigh", "max"])
    parser.add_argument("--max-tokens", type=int, required=True)
    parser.add_argument("--max-attempts", type=int, required=True, help="attempts per run before the benchmark stops")
    parser.add_argument("--run-timeout-s", type=float, required=True, help="the wall-clock limit of one run")
    parser.add_argument("--arm", choices=["semantic", "sql", SCHEMA_ONLY],
                        help=f"one arm only; {SCHEMA_ONLY} runs only when named and needs its own --out-dir")
    parser.add_argument("--out-dir", type=Path, help="where the run is written; default results/")
    parser.add_argument("--only", nargs="+", metavar="ID")
    args = parser.parse_args(argv)
    if args.min_repeats < 1:  # fewer repeats than the minimum is a pilot: the block shows counts only
        parser.error("--min-repeats must be at least 1")
    if args.max_attempts < 1:
        parser.error("--max-attempts must be at least 1")
    if args.first_repeat_eur <= 0:  # at 0 neither budget check can stop the first run
        parser.error("--first-repeat-eur must be above 0")

    questions = load_questions()
    if args.only:
        unknown = set(args.only) - {q.id for q in questions}
        if unknown:
            parser.error(f"no such question: {', '.join(sorted(unknown))}")
        questions = [q for q in questions if q.id in args.only]
    con = open_db(DEFAULT_DB)
    cursor = con.cursor()
    catalog = load_catalog(cursor)
    [(seed,)] = cursor.execute("SELECT seed FROM dataset_info").fetchall()
    out_dir = args.out_dir or RESULTS
    if (out_dir / "results.json").exists():  # a recorded run is never overwritten
        parser.error(f"{out_dir} already holds a run; pass another --out-dir")
    schema_md = (ROOT / "docs" / "schema.md").read_text()
    if args.arm == SCHEMA_ONLY:
        arms = [sql_arm(con, schema_only_md(cursor, catalog, schema_md), name=SCHEMA_ONLY)]
    else:
        arms = [arm for arm in (semantic_arm(con, catalog), sql_arm(con, schema_md)) if args.arm in (None, arm.name)]
    results = run_benchmark(
        make_model(effort=args.effort, max_tokens=args.max_tokens), questions, load_truth(cursor), arms,
        repeats=args.repeats, budget_eur=args.budget_eur, eur_per_usd=args.eur_per_usd,
        min_repeats=args.min_repeats, first_repeat_eur=args.first_repeat_eur, effort=args.effort, seed=seed,
        run_timeout_s=args.run_timeout_s, max_attempts=args.max_attempts, out_dir=out_dir,
        callbacks=tracing_callbacks(),
    )
    print(f"{len(results['runs'])} runs written to {out_dir}")
    if results.get("stopped"):
        print(f"Stopped early, at {results['stopped']}")
    if results["failure"]:
        print(f"The benchmark failed: {results['failure']}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
