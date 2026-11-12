"""The README results block, generated from results/results.json and nothing else.

Run with: uv run python -m agentic_ems.benchmark.report --write-readme
(without the flag it prints the block).
"""

import argparse
import json
import math
import random
import sys
from pathlib import Path

from agentic_ems.benchmark.verify import CATEGORIES

ROOT = Path(__file__).resolve().parents[3]
START, END = "<!-- results:start -->", "<!-- results:end -->"
Z = 1.959964  # two-sided 95%
RESAMPLES = 10_000
BOOTSTRAP_SEED = 20261111
EXAMPLE_ROWS = 5


def wilson(correct: int, n: int) -> tuple[float, float]:
    """The Wilson score 95% interval for correct out of n."""
    p = correct / n
    denominator = 1 + Z * Z / n
    centre = (p + Z * Z / (2 * n)) / denominator
    half = Z * math.sqrt(p * (1 - p) / n + Z * Z / (4 * n * n)) / denominator
    return max(0.0, centre - half), min(1.0, centre + half)


def bootstrap(diffs: list[float]) -> tuple[float, float]:
    """The 95% percentile interval of the mean, resampling questions with a fixed seed."""
    rng = random.Random(BOOTSTRAP_SEED)
    means = sorted(sum(rng.choices(diffs, k=len(diffs))) / len(diffs) for _ in range(RESAMPLES))
    return means[int(0.025 * RESAMPLES)], means[int(0.975 * RESAMPLES) - 1]


def _pct(x: float) -> str:
    return f"{100 * x:.0f}%"


def _rows(answer: dict) -> str:
    rows = answer["rows"]
    lines = [", ".join(answer["columns"])] + [", ".join(map(str, row)) for row in rows[:EXAMPLE_ROWS]]
    if len(rows) > EXAMPLE_ROWS:
        lines.append(f"... {len(rows)} rows in all")
    return "\n".join(lines)


def build_block(results: dict) -> str:
    arms = list(results["versions"]["prompts_sha256"])
    questions = results["questions"]
    groups = list(dict.fromkeys(q["group"] for q in questions.values()))
    # Accuracy is over completed repeats only: a repeat cut short by an abort is left out.
    completed = 0
    while sum(r["repeat"] == completed + 1 for r in results["runs"]) == len(questions) * len(arms) > 0:
        completed += 1
    runs = [r for r in results["runs"] if r["repeat"] <= completed]
    enough = completed >= results["min_repeats"]

    def of(arm: str, group: str | None = None) -> list[dict]:
        return [r for r in runs if r["arm"] == arm and group in (None, r["group"])]

    wrong = ", ".join(
        f"{arm} {sum(r['outcome'] in ('known_wrong', 'other_wrong') for r in of(arm))} of {len(of(arm))} runs"
        for arm in arms
    )
    lines = [(f"**Runs that delivered a wrong value without warning** (known wrong plus other wrong, of the runs "
              f"scored): {wrong}.")]
    if results["failure"]:
        lines += ["", f"The run failed: {results['failure']}."]
    versions = results["versions"]
    lines += ["", (f"{completed} of {results['repeats']} repeats completed, {len(questions)} questions, model "
                   f"{versions['model']}, effort {versions['effort']}.")]
    if results["stopped"]:
        lines += ["", f"The benchmark stopped early, at {results['stopped']}."]
    counted = sum(r["cost_eur"] for r in runs)
    incomplete = sum(r["cost_eur"] for r in results["runs"] if r["repeat"] > completed)
    lines += ["", (f"Spend: {counted:.2f} EUR in the runs the table counts, {incomplete:.2f} EUR in runs of an "
                   f"incomplete repeat and {results['unscored_eur']:.2f} EUR in attempts that were not scored, of a "
                   f"budget of {results['budget_eur']:.2f} EUR.")]
    if running := results["attempts_still_running"]:
        lines[-1] += (f" {running} abandoned attempt{'s were' if running != 1 else ' was'} still running at the "
                      "final save; their spend is not included.")
    if not enough:
        lines += ["", (f"This run is below the minimum of {results['min_repeats']} repeats: the block shows counts "
                       "only, and no comparison between the arms is claimed.")]

    lines += ["", ("| Arm | Group | Correct | Refusals | Known wrong | Other wrong | No answer | Cost EUR "
                   "| EUR per correct | Earlier call correct |"), "|---|---|---|---|---|---|---|---|---|---|"]
    for arm in arms:
        for group in groups:
            rs = of(arm, group)
            n, correct = len(rs), sum(r["correct"] for r in rs)
            accuracy = f"{correct} of {n}"
            if enough and group != "unanswerable":
                lo, hi = wilson(correct, n)
                accuracy += f", {_pct(correct / n)} ({_pct(lo)} to {_pct(hi)})"
            known = ", ".join(
                f"{c} {count}" for c in sorted(CATEGORIES) if (count := sum(r["category"] == c for r in rs))
            ) or "0"
            refusals, other, none = (sum(r["outcome"] == o for r in rs) for o in ("refusal", "other_wrong", "no_answer"))
            cost = sum(r["cost_eur"] for r in rs)
            per_correct = f"{cost / correct:.3f}" if correct else "none correct"
            lines.append(
                f"| {arm} | {group} | {accuracy} | {refusals} | {known} | {other} | {none} | {cost:.2f} "
                f"| {per_correct} | {sum(r['earlier_correct'] for r in rs)} |"
            )

    def accuracy_of(qid: str, arm: str) -> float:
        rs = [r for r in of(arm) if r["question"] == qid]
        return sum(r["correct"] for r in rs) / len(rs)

    if enough and len(arms) == 2:
        a, b = arms
        lines += ["", f"**Difference between arms**, {a} minus {b}, in accuracy points, paired by question:", "",
                  "| Group | Difference | 95% interval |", "|---|---|---|"]
        for group in groups:
            if group == "unanswerable":
                continue
            diffs = [accuracy_of(qid, a) - accuracy_of(qid, b) for qid, q in questions.items() if q["group"] == group]
            lo, hi = bootstrap(diffs)
            lines.append(f"| {group} | {100 * sum(diffs) / len(diffs):+.0f} | {100 * lo:+.0f} to {100 * hi:+.0f} |")

    example = next((r for r in results["runs"] if r["category"] == "double_count"), None)
    if example is None:
        lines += ["", ("No run produced a `double_count` value. What remains is in the table: refusals, cost, and "
                       "that the semantic arm cannot build the double-counting query while the text-to-SQL arm did "
                       "not build it this time.")]
    else:
        question = questions[example["question"]]
        lines += [
            "", (f"**Example**, a double count from a recorded run ({example['arm']} arm, question "
                 f"{example['question']}, repeat {example['repeat']}):"), "",
            "> " + question["text"].split("\n\n")[0], "",
            "The SQL that ran:", "", "```sql", example["sql"], "```", "",
            "The value it returned:", "", "```", _rows(example["answer"]), "```", "",
            "The reference value:", "", "```", _rows(question["reference"]), "```", "",
            f"[The transcript of this run](results/{example['transcript']})",
        ]

    if enough:
        lines += ["", ("Intervals: Wilson score 95% for accuracy, a 95% bootstrap over questions (10,000 resamples, "
                       "fixed seed) for differences.")]
    lines += ["", "Known wrong values are examples, not a complete list, so their counts are lower bounds."]
    return "\n".join(lines)


def write_readme(readme: Path, block: str) -> None:
    if not readme.exists():
        raise SystemExit(f"{readme} does not exist; it needs the lines {START} and {END} around the results block")
    text = readme.read_text()
    start, end = text.find(START), text.find(END)
    if start < 0 or end < start:
        raise SystemExit(f"{readme} lacks the markers {START} then {END} around the results block")
    readme.write_text(text[: start + len(START)] + "\n" + block + "\n" + text[end:])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate the README results block from results/results.json.")
    parser.add_argument("--write-readme", action="store_true", help="replace the marked block in README.md")
    args = parser.parse_args(argv)
    block = build_block(json.loads((ROOT / "results" / "results.json").read_text()))
    if args.write_readme:
        write_readme(ROOT / "README.md", block)
    else:
        print(block)
    return 0


if __name__ == "__main__":
    sys.exit(main())
