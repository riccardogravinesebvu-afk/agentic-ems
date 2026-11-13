"""Paid: run the stress questions once on the semantic arm and once on the sql arm, and save each run.

uv run python results/stress/run_stress.py --cap-eur CAP --eur-per-usd RATE [--only S1,S2]
Each run is saved as runs/<id>-<arm>.json; a run already saved is not run again. No retry, no score.
"""

import argparse
import json
import os
import sys
import tomllib
from dataclasses import asdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
HEADROOM_EUR = 0.20  # no run starts with less than this left under the cap; a run has cost about 0.02


def main() -> int:
    os.environ["LANGSMITH_TRACING"] = "false"  # before any LangChain import: no transcript leaves the machine
    os.environ["LANGCHAIN_TRACING_V2"] = "false"
    from agentic_ems.agent import make_model, run_agent, semantic_arm
    from agentic_ems.benchmark.run import Meter, _dump
    from agentic_ems.benchmark.sql_arm import sql_arm
    from agentic_ems.semantic import DEFAULT_DB, load_catalog, open_db

    parser = argparse.ArgumentParser()
    parser.add_argument("--cap-eur", type=float, required=True)
    parser.add_argument("--eur-per-usd", type=float, required=True)
    parser.add_argument("--only", default="")
    args = parser.parse_args()
    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("This makes paid API calls: set ANTHROPIC_API_KEY first.")
    if args.eur_per_usd <= 0:
        parser.error("--eur-per-usd must be positive")
    questions = tomllib.loads((HERE / "questions.toml").read_text())["question"]
    only = set(filter(None, args.only.split(",")))
    if unknown := only - {q["id"] for q in questions}:
        parser.error(f"unknown question ids: {sorted(unknown)}")
    con = open_db(DEFAULT_DB)
    with con.cursor() as cursor:
        catalog = load_catalog(cursor)
    arms = [semantic_arm(con, catalog), sql_arm(con, (HERE.parents[1] / "docs" / "schema.md").read_text())]
    model = make_model(effort="medium", max_tokens=16000)  # the settings of the recorded benchmark runs
    (HERE / "runs").mkdir(exist_ok=True)
    # The cap covers every start of this script: what the saved runs cost counts as spent.
    # shortcut: a run that raises is not saved, so its cost is forgotten on the next start; it is printed below.
    spent = sum(json.loads(p.read_text())["cost_eur"] for p in (HERE / "runs").glob("*.json"))
    for q in questions:
        for arm in arms:
            out = HERE / "runs" / f"{q['id']}-{arm.name}.json"
            if (only and q["id"] not in only) or out.exists():
                continue
            if spent > args.cap_eur - HEADROOM_EUR:
                print(f"Stopped before {out.name}: {spent:.4f} EUR spent, cap {args.cap_eur:.2f}.")
                return 1
            meter = Meter(args.eur_per_usd)
            try:
                record = run_agent(model, arm, q["text"], callbacks=[meter])
            except Exception as exc:  # noqa: BLE001 - the money already spent is reported before the stop
                spent += meter.cost
                print(f"{out.name}: failed ({type(exc).__name__}: {exc}), {meter.cost:.4f} EUR; {spent:.4f} EUR spent.")
                return 1
            spent += meter.cost
            _dump(out, {"question": q, "record": asdict(record), "cost_eur": meter.cost})
            print(f"{out.name}: {record.final}, {len(record.calls)} calls, {meter.cost:.4f} EUR")
    print(f"Spent {spent:.4f} EUR.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
