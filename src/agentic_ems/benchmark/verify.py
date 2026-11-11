"""Rebuild the database from the seed and check every reference answer against the second computation.

Run with: uv run python -m agentic_ems.benchmark.verify
"""

import math
import sys
import tomllib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from agentic_ems.benchmark.reference_py import ANSWERS, Data
from agentic_ems.generator import generate
from agentic_ems.semantic import DEFAULT_DB, execute_limited, load_catalog, open_db

HERE = Path(__file__).resolve().parent
QUESTIONS_FILE = HERE / "questions.toml"
REFERENCE_SQL = HERE / "reference_sql.toml"
CATEGORIES = {"double_count", "children_sum", "separate_peaks", "extra_rows"}
COLUMN_FORMS = {
    "meter_id": "meter_id, the meter's id as shown in the meter tree, not its name",
    "bucket_start": "bucket_start, the start of the time bucket in plant local time as ISO 8601 text with the UTC "
    "offset, for example 2026-10-25T02:00:00+01:00",
    "tariff_band": "tariff_band, which is F1, F2 or F3",
}


@dataclass
class Question:
    id: str
    group: str
    text: str
    keys: list[str]
    source: str
    mistake: str
    shape_note: str | None = None


@dataclass
class Truth:
    reference: tuple[list[str], list[tuple]] | None
    wrong: list[tuple[str, list[str], list[tuple]]]


def shape_line(keys: list[str]) -> str:
    """The fixed answer-format sentence appended to every question, the same for both arms."""
    if not keys:
        return "Answer with a single row with one column, value, which is one number."
    columns = "; ".join([COLUMN_FORMS[key] for key in keys] + ["value, which is one number"])
    return f"Answer with one row per {' and '.join(keys)}, with these columns in this order: {columns}."


def load_questions() -> list[Question]:
    questions = tomllib.loads(QUESTIONS_FILE.read_text())["question"]
    return [
        Question(
            q["id"], q["group"], q["ask"] + "\n\n" + shape_line(q["keys"]), q["keys"], q["source"], q["mistake"],
            q.get("shape_note"),
        )
        for q in questions
    ]


def load_truth(con) -> dict[str, Truth]:
    """Run every reference query and every known wrong query; unanswerable questions get no reference."""
    entries = tomllib.loads(REFERENCE_SQL.read_text())
    truth = {}
    for q in load_questions():
        entry = entries.get(q.id)
        if entry is None:
            truth[q.id] = Truth(None, [])
            continue
        wrong = [(w["category"], *execute_limited(con, w["sql"])) for w in entry.get("wrong", [])]
        truth[q.id] = Truth(execute_limited(con, entry["sql"]), wrong)
    return truth


def _key(column: str, value):
    if column == "bucket_start" and isinstance(value, str):
        value = datetime.fromisoformat(value)
    if isinstance(value, datetime) and value.tzinfo is not None:
        return value.timestamp()  # compare instants, not text
    return value


def _by_key(columns: list[str], rows: list[tuple], keys: list[str]) -> dict | None:
    if not {*keys, "value"} <= set(columns):
        return None
    index = [columns.index(k) for k in keys]
    value = columns.index("value")
    found = {tuple(_key(k, row[i]) for k, i in zip(keys, index)): row[value] for row in rows}
    return found if len(found) == len(rows) else None  # a repeated key is a row the reference does not have


def _same(value, reference) -> bool:
    if value is None or reference is None:
        return value is None and reference is None
    value, reference = float(value), float(reference)
    if reference == 0:
        return abs(value) <= 0.001
    return math.isclose(value, reference, rel_tol=0.001)


# shortcut: local stand-in with the scorer's numbers; replace with the scorer's function when it exists.
def rows_agree(columns: list[str], rows: list[tuple], ref_columns: list[str], ref_rows: list[tuple], keys) -> bool:
    """Rows matched by key columns (bucket_start as an instant), values within 0.1% (0.001 at zero), order ignored."""
    mine, reference = _by_key(columns, rows, keys), _by_key(ref_columns, ref_rows, keys)
    if mine is None or reference is None or mine.keys() != reference.keys():
        return False
    return all(_same(mine[k], reference[k]) for k in reference)


def main() -> int:
    generate(DEFAULT_DB)
    con = open_db(DEFAULT_DB)
    data = Data(con.cursor(), load_catalog(con))
    truth = load_truth(con)
    failures = checked = 0
    for q in load_questions():
        reference = truth[q.id].reference
        if reference is None:
            print(f"{q.id} no reference (unanswerable)")
            continue
        checked += 1
        agree = rows_agree(*ANSWERS[q.id](data), *reference, q.keys)
        clashes = [c for c, columns, rows in truth[q.id].wrong if rows_agree(columns, rows, *reference, q.keys)]
        line = f"{q.id} {'agree' if agree else 'DISAGREE'} {len(reference[1])} rows"
        if clashes:
            line += f"; wrong answer equals reference: {', '.join(clashes)}"
        print(line)
        failures += not agree or bool(clashes)
    print(f"{checked - failures} of {checked} answerable questions verified")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
