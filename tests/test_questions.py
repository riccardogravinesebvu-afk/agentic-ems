import re
import tomllib
from datetime import date, timedelta
from pathlib import Path

from agentic_ems.generator import build_meters

ROOT = Path(__file__).resolve().parents[1]
QUESTIONS = tomllib.loads((ROOT / "src" / "agentic_ems" / "benchmark" / "questions.toml").read_text())["question"]
GROUPS = {"hierarchy", "control", "metric_definition", "out_of_scope", "unanswerable"}
KEYS = {"meter_id", "bucket_start", "tariff_band"}
FIRST, END = date(2025, 11, 1), date(2026, 11, 1)
MONTHS = [date(2026, m, 1).strftime("%B").lower() for m in range(1, 13)]
DAY = re.compile(rf"\b(\d{{1,2}}) ({'|'.join(MONTHS)}) (\d{{4}})\b", re.IGNORECASE)
MONTH = re.compile(rf"\b({'|'.join(MONTHS)}) (\d{{4}})\b", re.IGNORECASE)
NUMERIC = re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b")  # day/month/year


def test_ids_unique_and_fields_in_closed_sets():
    ids = [q["id"] for q in QUESTIONS]
    assert len(ids) == len(set(ids))
    assert {q["group"] for q in QUESTIONS} == GROUPS
    for q in QUESTIONS:
        assert set(q["keys"]) <= KEYS, q["id"]
        assert re.fullmatch(r"public standard|author, from practice|error class P\d+", q["source"]), q["id"]
        assert q["mistake"], q["id"]


def test_ask_names_no_meter_id_column_or_value():
    # Only identifier-like column names are banned: plain words such as kWh, units and product
    # are allowed in an ask on purpose, because people use them.
    # solar and battery are meter ids that are also the plain words people use for those meters.
    meter_ids = [m[0] for m in build_meters()[0] if m[0] not in ("solar", "battery")]
    schema = (ROOT / "docs" / "schema.md").read_text()
    columns = {c for _, c in re.findall(r"`(\w+)\.(\w+)`", schema) if "_" in c} | {"bucket_start"}
    for q in QUESTIONS:
        words = set(re.findall(r"\w+", q["ask"].lower()))
        assert not words & {*meter_ids, *columns, "value", "values", "sql"}, q["id"]


def test_no_two_asks_share_their_first_four_words():
    starts = [tuple(re.findall(r"[\w'/]+", q["ask"].lower())[:4]) for q in QUESTIONS]
    assert len(starts) == len(set(starts))


def test_every_date_lies_inside_the_dataset():
    for q in QUESTIONS:
        if q["group"] == "unanswerable":
            continue
        ask = q["ask"]
        days = [date(int(y), MONTHS.index(m.lower()) + 1, int(d)) for d, m, y in DAY.findall(ask)]
        days += [date(int(y), int(m), int(d)) for d, m, y in NUMERIC.findall(ask)]
        for m, y in MONTH.findall(ask):
            month = MONTHS.index(m.lower()) + 1
            days += [date(int(y), month, 1), date(int(y) + month // 12, month % 12 + 1, 1) - timedelta(days=1)]
        assert all(FIRST <= d < END for d in days), q["id"]
        # A year left after removing the dates above is a date this test could not read.
        assert not re.search(r"\b\d{4}\b", NUMERIC.sub("", MONTH.sub("", ask))), q["id"]


QUANTITY = re.compile(r"(?<![\d:,.])(\d[\d,.]*\d|\d)\s*(?:kWh|kW|h|EUR|%|hours|units|pieces)(?!\w)")


def asked_numbers(text: str) -> set[str]:
    return {n.replace(",", "") for n in re.findall(r"\d[\d,.]*", text)}


def test_notes_hold_no_computed_number():
    # The two reference computations are written separately, so no note may give part of an answer.
    for q in QUESTIONS:
        allowed = asked_numbers(q["ask"])
        for text in (q.get("shape_note", ""), q["mistake"]):
            for number in QUANTITY.findall(text):
                assert number.replace(",", "") in allowed, (q["id"], number)


def test_comments_hold_no_computed_number():
    raw = (ROOT / "src" / "agentic_ems" / "benchmark" / "questions.toml").read_text()
    allowed = asked_numbers(" ".join(q["ask"] for q in QUESTIONS))
    for number in QUANTITY.findall(raw):
        assert number.replace(",", "") in allowed, number


def test_unanswerable_group_size():
    assert 3 <= sum(q["group"] == "unanswerable" for q in QUESTIONS) <= 8
