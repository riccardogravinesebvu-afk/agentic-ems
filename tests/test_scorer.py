import tomllib
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from agentic_ems.agent import RunRecord
from agentic_ems.benchmark.scorer import rows_match, score
from agentic_ems.benchmark.verify import (
    REFERENCE_SQL,
    Question,
    Truth,
    load_questions,
    load_truth,
)
from agentic_ems.semantic import MetricRequest, load_catalog, open_db, query, validate

QUESTIONS = load_questions()
REQUESTS = {qid: entry["request"] for qid, entry in tomllib.loads(REFERENCE_SQL.read_text()).items() if "request" in entry}
# Ids where the compiler disagrees with the reference, each awaiting the controller's ruling.
AWAITING: set[str] = set()


def record(final="rows", columns=None, rows=None, successful=None, cause=None) -> RunRecord:
    if successful is None:
        successful = [(columns, rows)] if final == "rows" else []
    return RunRecord(
        arm="test", question="q", messages=[], calls=[], final=final, columns=columns, rows=rows,
        successful_results=successful, refusal_reason="no such data" if final == "refusal" else None,
        cause=cause, usage={}, served_model="test",
    )


def question(group="hierarchy", keys=("bucket_start",)) -> Question:
    return Question("T1", group, "text", list(keys), "test", "test")


REF = (["bucket_start", "value"], [("2026-10-25T02:00:00+02:00", 100.0), ("2026-10-25T02:00:00+01:00", 0.0)])
TRUTH = Truth(REF, [("double_count", ["bucket_start", "value"], [("2026-10-25T02:00:00+02:00", 105.0),
                                                                ("2026-10-25T02:00:00+01:00", 0.0)])])


def outcome(columns, rows, q=None, truth=TRUTH):
    return score(q or question(), truth, record(columns=columns, rows=rows)).outcome


def one_value(value):
    """Score a single-value answer against a single-value reference of 100."""
    return outcome(["value"], [(value,)], question(keys=()), Truth((["value"], [(100.0,)]), []))


@pytest.fixture(scope="module")
def full_con(full_db_path):
    return open_db(full_db_path)


@pytest.fixture(scope="module")
def truth(full_con):
    return load_truth(full_con)


def reference_record(q, truth):
    reference = truth[q.id].reference
    return record("refusal") if reference is None else record(columns=reference[0], rows=reference[1])


def test_reference_scores_correct_for_every_question(truth):
    scores = [score(q, truth[q.id], reference_record(q, truth)) for q in QUESTIONS]
    assert [s.outcome for s in scores].count("refusal") == sum(q.group == "unanswerable" for q in QUESTIONS)
    assert all(s.correct for s in scores), [q.id for q, s in zip(QUESTIONS, scores) if not s.correct]


@pytest.mark.parametrize(
    "qid",
    [
        pytest.param(qid, marks=pytest.mark.xfail(strict=True, reason=f"{qid}: awaiting controller ruling"))
        if qid in AWAITING
        else qid
        for qid in REQUESTS
    ],
)
def test_compiler_agrees_with_every_reference(qid, truth, full_con):
    q = next(q for q in QUESTIONS if q.id == qid)
    catalog = load_catalog(full_con.cursor())
    request = MetricRequest(**REQUESTS[qid])
    validate(request, catalog)
    result = query(request, full_con.cursor(), catalog)
    assert score(q, truth[qid], record(columns=result.columns, rows=result.rows)).outcome == "correct"


def test_empty_and_constant_answers_score_zero(truth):
    for q in QUESTIONS:
        reference = truth[q.id].reference
        columns = [*q.keys, "value"]
        runs = [record(columns=columns, rows=[]), record(columns=columns, rows=[(*[None] * len(q.keys), 0.0)])]
        if reference is not None:
            # A constant answer on exactly the reference's keys.
            keys = [reference[0].index(k) for k in q.keys]
            for constant in (0, 1.0, 1000.0):
                runs.append(record(columns=columns, rows=[(*(row[i] for i in keys), constant) for row in reference[1]]))
        assert not any(score(q, truth[q.id], run).correct for run in runs), q.id


def test_each_known_wrong_lands_in_its_category(truth):
    checked = 0
    for q in QUESTIONS:
        for category, columns, rows in truth[q.id].wrong:
            s = score(q, truth[q.id], record(columns=columns, rows=rows))
            assert (s.outcome, s.category, s.correct) == ("known_wrong", category, False), (q.id, category)
            checked += 1
    assert checked > 0


def test_tolerance_edges():
    assert one_value(100.09) == one_value(99.91) == "correct"
    assert one_value(100.11) == one_value(99.89) == "other_wrong"
    assert one_value(100.1001) == "other_wrong"  # relative to the reference, not to the larger value
    at_zero = (question(keys=()), Truth((["value"], [(0.0,)]), []))
    assert outcome(["value"], [(0.0005,)], *at_zero) == outcome(["value"], [(-0.0005,)], *at_zero) == "correct"
    assert outcome(["value"], [(0.0011,)], *at_zero) == "other_wrong"
    half = (question(keys=()), Truth((["value"], [(0.5,)]), []))
    assert outcome(["value"], [(0.5008,)], *half) == "other_wrong"
    assert outcome(["value"], [(0.5004,)], *half) == "correct"
    assert one_value(100) == one_value(Decimal("100.05")) == "correct"


def test_null_matches_only_null():
    null = (question(keys=()), Truth((["value"], [(None,)]), []))
    assert outcome(["value"], [(None,)], *null) == "correct"
    assert outcome(["value"], [(0.0,)], *null) == "other_wrong"
    assert one_value(None) == "other_wrong"


def test_extra_columns_ignored_missing_column_other_wrong():
    assert outcome(["note", "value", "bucket_start"], [("a", v, b) for b, v in REF[1]]) == "correct"
    assert outcome(["bucket_start"], [(b,) for b, _ in REF[1]]) == "other_wrong"
    assert outcome(["value"], [(v,) for _, v in REF[1]]) == "other_wrong"
    assert outcome(["bucket_start", "kwh"], REF[1]) == "other_wrong"


def test_row_order_ignored():
    assert outcome(REF[0], list(reversed(REF[1]))) == "correct"


def test_offset_forms_of_same_instant_match():
    instants = ["2026-10-25T00:00:00+00:00", "2026-10-25T01:00:00Z"]
    assert outcome(["value", "bucket_start"], [(100.05, instants[0]), (0.0005, instants[1])]) == "correct"
    native = [datetime(2026, 10, 25, 0, tzinfo=UTC), datetime(2026, 10, 25, 2, tzinfo=timezone(timedelta(hours=1)))]
    assert outcome(REF[0], [(native[0], 100.0), (native[1], 0.0)]) == "correct"
    # The same wall-clock text with the other offset is another instant.
    assert outcome(REF[0], [("2026-10-25T02:00:00+01:00", 100.0), ("2026-10-25T02:00:00+02:00", 0.0)]) == "other_wrong"


def test_extra_or_missing_rows_never_correct():
    assert outcome(REF[0], REF[1][:1]) == "other_wrong"
    assert outcome(REF[0], [*REF[1], ("2026-10-25T03:00:00+01:00", 1.0)]) == "other_wrong"
    assert outcome(REF[0], [*REF[1], ("2026-10-25T03:00:00+01:00", None)]) == "other_wrong"


def test_unexpected_shapes_do_not_raise():
    by_meter = question(keys=("meter_id",))
    meter_truth = Truth((["meter_id", "value"], [("line_a1", 1.0)]), [])
    bad = [
        (REF[0], [*REF[1], REF[1][0]]),  # duplicate key
        (REF[0], [(REF[1][0][0], "100"), REF[1][1]]),  # text value
        (REF[0], [(REF[1][0][0], float("nan")), REF[1][1]]),
        (REF[0], [(REF[1][0][0], float("inf")), REF[1][1]]),
        (REF[0], [(REF[1][0][0], Decimal("NaN")), REF[1][1]]),
        (REF[0], [(REF[1][0][0], Decimal("sNaN")), REF[1][1]]),
        (REF[0], [(REF[1][0][0], True), REF[1][1]]),
        (REF[0], [(REF[1][0][0], 10**400), REF[1][1]]),
        (REF[0], []),  # zero rows
        (REF[0], [("2026-10-25T02:00:00", 100.0), REF[1][1]]),  # no offset
        (REF[0], [(datetime(2026, 10, 25, 2), 100.0), REF[1][1]]),  # native, no time zone
        (REF[0], [("not a time", 100.0), REF[1][1]]),
        (REF[0], [(None, 100.0), REF[1][1]]),
        (REF[0], [(20261025, 100.0), REF[1][1]]),
        (REF[0], [(REF[1][0][0],), REF[1][1]]),  # short row
        (REF[0], [{"bucket_start": 1}, REF[1][1]]),
        (REF[0], None),
        (None, REF[1]),
        ("bucket_start,value", REF[1]),
        (["bucket_start", "value", "value"], [(b, v, v) for b, v in REF[1]]),  # ambiguous value column
    ]
    for columns, rows in bad:
        assert outcome(columns, rows) == "other_wrong", (columns, rows)
    for meter in (1, None, ["line_a1"]):
        assert outcome(["meter_id", "value"], [(meter, 1.0)], by_meter, meter_truth) == "other_wrong"
    assert outcome(["meter_id", "value"], [("line_a1", 1.0)], by_meter, meter_truth) == "correct"
    assert outcome(["meter_id", "value"], [("LINE_A1", 1.0)], by_meter, meter_truth) == "other_wrong"


def test_reference_without_rows_is_never_matched():
    empty = Truth((["value"], []), [])
    assert outcome(["value"], [], question(keys=()), empty) == "other_wrong"
    assert not rows_match(["value"], [], ["value"], [], [])


def test_refusal_correct_only_when_unanswerable():
    for group in ("hierarchy", "control", "metric_definition", "out_of_scope"):
        s = score(question(group), TRUTH, record("refusal"))
        assert (s.outcome, s.correct) == ("refusal", False), group
    s = score(question("unanswerable"), Truth(None, []), record("refusal"))
    assert (s.outcome, s.category, s.correct, s.earlier_correct) == ("refusal", None, True, False)
    s = score(question("unanswerable"), Truth(None, []), record(columns=REF[0], rows=REF[1]))
    assert (s.outcome, s.correct) == ("other_wrong", False)


def test_no_answer_causes():
    for cause in ("max_tool_calls", "max_tokens", "provider_refusal", "no_tool_call"):
        for group in ("hierarchy", "unanswerable"):
            s = score(question(group), TRUTH, record("none", cause=cause))
            assert (s.outcome, s.category, s.correct) == ("no_answer", None, False), (cause, group)


def test_earlier_correct_flag():
    wrong = (REF[0], [(REF[1][0][0], 50.0), REF[1][1]])
    s = score(question(), TRUTH, record(columns=wrong[0], rows=wrong[1], successful=[REF, wrong]))
    assert (s.outcome, s.earlier_correct) == ("other_wrong", True)
    s = score(question(), TRUTH, record(columns=REF[0], rows=REF[1], successful=[wrong, REF]))
    assert (s.outcome, s.correct, s.earlier_correct) == ("correct", True, False)
    s = score(question(), TRUTH, record(columns=wrong[0], rows=wrong[1], successful=[wrong]))
    assert s.earlier_correct is False
    s = score(question(), TRUTH, record("refusal", successful=[REF]))
    assert (s.outcome, s.earlier_correct) == ("refusal", True)  # correct rows, then a refusal
    s = score(question(), TRUTH, record("none", successful=[REF], cause="max_tokens"))
    assert (s.outcome, s.earlier_correct) == ("no_answer", True)  # correct rows, then cut off
    s = score(question(), TRUTH, record("none", successful=[wrong], cause="max_tokens"))
    assert s.earlier_correct is False
    s = score(question("unanswerable"), Truth(None, []), record("refusal", successful=[REF, wrong]))
    assert s.earlier_correct is False


def test_rows_match_rules():
    keys = ["bucket_start"]
    same_instant = ["2026-10-25T00:00:00+00:00", "2026-10-25T01:00:00+00:00"]
    assert rows_match(["value", "bucket_start"], [(100.05, same_instant[0]), (0.0005, same_instant[1])], *REF, keys)
    assert not rows_match(REF[0], [("2026-10-25T02:00:00+02:00", 100.2), REF[1][1]], *REF, keys)
    assert not rows_match(REF[0], [REF[1][0], ("2026-10-25T02:00:00+01:00", 0.002)], *REF, keys)
    assert not rows_match(REF[0], REF[1][:1], *REF, keys)
    assert not rows_match(REF[0], [*REF[1], ("2026-10-25T03:00:00+01:00", 1.0)], *REF, keys)
    assert not rows_match(REF[0], [*REF[1], REF[1][0]], *REF, keys)
    assert not rows_match(["bucket_start"], [(r[0],) for r in REF[1]], *REF, keys)
    assert rows_match(["value"], [(None,)], ["value"], [(None,)], [])
    assert not rows_match(["value"], [(0.0,)], ["value"], [(None,)], [])
    assert not rows_match(["value"], [(None,)], ["value"], [(0.0,)], [])
