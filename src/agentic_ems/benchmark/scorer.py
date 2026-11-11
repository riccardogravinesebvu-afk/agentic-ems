"""Turn one benchmark run into exactly one outcome, from the rows it returned and nothing else."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from agentic_ems.agent import RunRecord
    from agentic_ems.benchmark.verify import Question, Truth

REL_TOL = 0.001
ABS_TOL_AT_ZERO = 0.001
Outcome = Literal["correct", "known_wrong", "other_wrong", "refusal", "no_answer"]


@dataclass
class Score:
    outcome: Outcome
    category: str | None
    correct: bool
    earlier_correct: bool


def _key(column: str, value):
    if column != "bucket_start":
        if not isinstance(value, str):
            raise TypeError(f"{column} is not text")
        return value
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError("bucket_start is not an instant with a UTC offset")
    return value.timestamp()  # compare instants, not text


def _number(value) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float | Decimal):
        raise TypeError("value is not a number")
    number = float(value)  # raises for a signalling NaN or an int too large for a float
    if not math.isfinite(number):
        raise ValueError("value is not finite")
    return number


def _table(columns, rows, keys: list[str]) -> dict | None:
    """Key tuple to value, or None when the rows cannot be read in the question's shape."""
    try:
        # SQL identifiers ignore case, so `AS Value` names the value column; the values stay exact.
        names = [n.casefold() if isinstance(n, str) else n for n in columns]
        if any(names.count(name) != 1 for name in [*keys, "value"]):
            return None
        index = [names.index(key) for key in keys]
        value = names.index("value")
        table: dict = {}
        for row in rows:
            key = tuple(_key(k, row[i]) for k, i in zip(keys, index))
            if key in table:
                return None  # a repeated key is a row the reference does not have
            table[key] = _number(row[value])
    except (TypeError, ValueError, IndexError, KeyError, OverflowError):
        return None
    return table


def _same(value: float | None, reference: float | None) -> bool:
    if value is None or reference is None:
        return value is None and reference is None
    if reference == 0:
        return abs(value) <= ABS_TOL_AT_ZERO
    return abs(value - reference) <= REL_TOL * abs(reference)


def rows_match(columns, rows, ref_columns, ref_rows, keys: list[str]) -> bool:
    """Rows matched by key columns (bucket_start as an instant), values within the tolerance, order ignored."""
    mine, reference = _table(columns, rows, keys), _table(ref_columns, ref_rows, keys)
    if not reference or mine is None or mine.keys() != reference.keys():
        return False  # a reference without rows is never matched
    return all(_same(mine[key], reference[key]) for key in reference)


def score(question: Question, truth: Truth, record: RunRecord) -> Score:
    unanswerable = question.group == "unanswerable"
    reference = truth.reference

    def is_reference(columns, rows) -> bool:
        return reference is not None and rows_match(columns, rows, *reference, question.keys)

    category = None
    outcome: Outcome
    if record.final == "refusal":
        outcome = "refusal"
    elif record.final != "rows":
        outcome = "no_answer"
    elif unanswerable:
        outcome = "other_wrong"
    elif is_reference(record.columns, record.rows):
        outcome = "correct"
    else:
        category = next(
            (c for c, columns, rows in truth.wrong if rows_match(record.columns, record.rows, columns, rows, question.keys)),
            None,
        )
        outcome = "other_wrong" if category is None else "known_wrong"
    correct = outcome == "correct" or (outcome == "refusal" and unanswerable)
    # Results the run did not end on: all of them, unless the run ended on the last one's rows.
    abandoned = record.successful_results[:-1] if record.final == "rows" else record.successful_results
    earlier = outcome != "correct" and not unanswerable and any(is_reference(columns, rows) for columns, rows in abandoned)
    return Score(outcome, category, correct, earlier)
