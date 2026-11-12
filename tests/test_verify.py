import random
import tomllib
from datetime import date

import pytest

from agentic_ems.benchmark.reference_py import ANSWERS, Data
from agentic_ems.benchmark.scorer import rows_match
from agentic_ems.benchmark.verify import (
    CATEGORIES,
    REFERENCE_SQL,
    load_questions,
    load_truth,
    shape_line,
)
from agentic_ems.generator import build_meters
from agentic_ems.semantic import load_catalog, open_db

QUESTIONS = load_questions()
ANSWERABLE = [q for q in QUESTIONS if q.group != "unanswerable"]


@pytest.fixture(scope="module")
def full_con(full_db_path):
    return open_db(full_db_path)


@pytest.fixture(scope="module")
def truth(full_con):
    return load_truth(full_con)


@pytest.fixture(scope="module")
def data(full_con):
    return Data(full_con.cursor(), load_catalog(full_con))


@pytest.mark.parametrize("question", [pytest.param(q, id=q.id) for q in ANSWERABLE])
def test_both_computations_agree(question, truth, data):
    reference = truth[question.id].reference
    assert reference is not None
    assert rows_match(*ANSWERS[question.id](data), *reference, question.keys)


def test_every_answerable_question_has_both_answers_and_no_other_does():
    ids = set(tomllib.loads(REFERENCE_SQL.read_text()))
    answerable = {q.id for q in ANSWERABLE}
    assert ids == answerable == set(ANSWERS)
    assert len(answerable) < len(QUESTIONS)


def test_every_reference_has_rows(truth):
    for q in ANSWERABLE:
        reference = truth[q.id].reference
        assert reference is not None and reference[1], q.id


def test_wrong_categories_closed_and_every_hierarchy_question_has_one(truth):
    for q in QUESTIONS:
        categories = [category for category, _, _ in truth[q.id].wrong]
        assert set(categories) <= CATEGORIES, q.id
        if q.group == "hierarchy":
            assert categories, q.id


def test_every_wrong_answer_differs_from_its_reference(truth):
    for q in ANSWERABLE:
        reference = truth[q.id].reference
        assert reference is not None
        for category, columns, rows in truth[q.id].wrong:
            assert not rows_match(columns, rows, *reference, q.keys), (q.id, category)


def test_site_day_equals_machines_times_the_margins_on_each_path(data):
    # The generator draws each parent's margin first from its seeded generator, so the margins are known.
    seed = data.cursor.execute("SELECT seed FROM dataset_info").fetchall()[0][0]
    rng = random.Random(seed)
    margins = {m[0]: rng.uniform(0.03, 0.08) for m in build_meters()[0] if m[2] in ("site", "department", "line")}
    day = (date(2026, 10, 25), date(2026, 10, 26))
    machines = data.under("site", "machine")
    expected = 0.0
    for reading in data.readings(machines, *day):
        line = data.catalog.meters[reading.meter_id].parent_id
        assert line is not None
        dept = data.catalog.meters[line].parent_id
        assert dept is not None
        expected += reading.kwh * (1 + margins[line]) * (1 + margins[dept]) * (1 + margins["site"])
    site = sum(reading.kwh for reading in data.readings(["site"], *day))
    assert site == pytest.approx(expected, rel=1e-4)


def test_question_text_is_the_ask_then_a_fixed_shape_line():
    for q in QUESTIONS:
        line = shape_line(q.keys)
        assert q.text.endswith("\n\n" + line), q.id
        for column in [*q.keys, "value"]:
            assert column in line, q.id
        assert not {"sql", "table", "tool"} & set(line.lower().replace(",", " ").replace(".", " ").split()), q.id
    assert shape_line([]) != shape_line(["meter_id"]) != shape_line(["bucket_start"])
