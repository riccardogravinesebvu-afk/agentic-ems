import json
from pathlib import Path

import pytest
from fakes import ScriptedChatModel, reply
from starlette.testclient import TestClient

from agentic_ems import demo
from agentic_ems.semantic import load_catalog, open_db, query

PERIOD = {"start": "2026-10-20", "end": "2026-10-23"}
DAILY = {"metric": "consumption_kwh", "node": "site", "grain": "day"} | PERIOD
BY_DEPT = {"metric": "consumption_kwh", "breakdown": "department"} | PERIOD
BY_LINE = {"metric": "peak_kw", "meters": ["line_a1", "line_b2"]} | PERIOD
Q = "How much electricity did the whole plant use each day?"


def events(body: str) -> list[tuple[str, dict]]:
    out = []
    for block in body.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.split("\n"))
        out.append((lines["event"], json.loads(lines["data"])))
    return out


def ask(con, model, question, monkeypatch, key="test-key"):
    if key:
        monkeypatch.setenv("ANTHROPIC_API_KEY", key)
    else:
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    client = TestClient(demo.build_app(con, lambda: model))
    response = client.post("/api/ask", json={"question": question})
    assert response.headers["content-type"].startswith("text/event-stream")
    return events(response.text)


def test_answered_question_streams_text_then_request_rows_done(con, monkeypatch):
    model = ScriptedChatModel(script=[reply(("query_metric", DAILY), text="Looking."), reply(text="Done.")])
    got = ask(con, model, Q, monkeypatch)
    names = [name for name, _ in got]
    assert names[-3:] == ["request", "rows", "done"] and set(names[:-3]) == {"text"}
    assert "".join(data["delta"] for name, data in got if name == "text").replace(" ", "") == "Looking.Done."
    expected = query(DAILY, con.cursor(), load_catalog(con))
    assert got[-3][1] == {"request": DAILY, "sql": expected.sql}
    assert got[-2][1] == {"columns": expected.columns, "rows": [list(row) for row in expected.rows]}
    assert f"Today is {load_catalog(con).end_date.isoformat()}." in model.received[0][0].content[0]["text"]


def test_refusal_streams_the_reason(con, monkeypatch):
    model = ScriptedChatModel(script=[reply(("refuse", {"reason": "Ranking is not a metric."}))])
    got = ask(con, model, Q, monkeypatch)
    assert got[-2:] == [("refusal", {"reason": "Ranking is not a metric."}), ("done", {})]


@pytest.mark.parametrize("question", ["", "   ", "x" * (demo.MAX_QUESTION_CHARS + 1), 42])
def test_empty_or_long_question_is_an_error_without_a_model_call(con, monkeypatch, question):
    model = ScriptedChatModel(script=[reply(text="never sent")])
    got = ask(con, model, question, monkeypatch)
    assert [name for name, _ in got] == ["error", "done"]
    assert model.received == []


def test_question_at_the_limit_is_accepted(con, monkeypatch):
    model = ScriptedChatModel(script=[reply(("refuse", {"reason": "no"}))])
    got = ask(con, model, "x" * demo.MAX_QUESTION_CHARS, monkeypatch)
    assert got[-2][0] == "refusal"


def test_missing_key_names_the_variable(con, monkeypatch):
    model = ScriptedChatModel(script=[reply(text="never sent")])
    got = ask(con, model, Q, monkeypatch, key=None)
    assert got[0][0] == "error" and "ANTHROPIC_API_KEY" in got[0][1]["message"]
    assert got[-1] == ("done", {}) and model.received == []


def test_a_failed_run_is_a_one_line_error_without_details(con, monkeypatch):
    def broken():
        raise RuntimeError("secret-key at /home/somewhere/file.py\nTraceback")

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    got = events(TestClient(demo.build_app(con, broken)).post("/api/ask", json={"question": Q}).text)
    assert [name for name, _ in got] == ["error", "done"]
    message = got[0][1]["message"]
    assert "\n" not in message and "secret" not in message and "/" not in message


def test_health_reports_only_whether_a_key_is_present(con, monkeypatch):
    client = TestClient(demo.build_app(con, lambda: None))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-very-secret")
    assert client.get("/api/health").json() == {"ok": True, "has_key": True}
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    assert client.get("/api/health").json() == {"ok": True, "has_key": False}


def test_server_never_exposes_run_sql(con, monkeypatch):
    model = ScriptedChatModel(script=[reply(("run_sql", {"sql": "SELECT 1"})), reply(text="Done.")])
    got = ask(con, model, Q, monkeypatch)
    assert [tool["name"] for tool in model.tools] == ["query_metric", "refuse"]
    assert "rows" not in [name for name, _ in got]  # the call was answered with an error, not run
    assert TestClient(demo.build_app(con, lambda: None)).post("/api/run_sql", json={}).status_code in (404, 405)


def check_sample(path: Path, con, catalog) -> None:
    """A saved sample replays exactly: `query` answers its request with its rows, or it is a refusal."""
    sample = json.loads(path.read_text())
    assert sample["question"] in demo.SAMPLE_QUESTIONS
    if sample["refusal"] is not None:
        assert sample["refusal"].strip()
        assert sample["request"] is None and sample["sql"] is None
        assert sample["columns"] is None and sample["rows"] is None
        return
    with con.cursor() as cursor:
        result = query(sample["request"], cursor, catalog)
    assert sample["sql"] == result.sql and sample["columns"] == result.columns
    assert sample["rows"] == json.loads(json.dumps(result.rows))


# Zero sample files give zero cases, which pytest reports as a skip with its reason.
@pytest.mark.parametrize("path", sorted(p for p in demo.SAMPLES.glob("*.json") if p.name != "index.json"),
                         ids=lambda p: p.name)
def test_saved_sample_replays_on_the_full_database(path, full_db_path):
    con = open_db(full_db_path)
    check_sample(path, con, load_catalog(con))


def test_record_writes_one_file_per_question_and_an_index(con, tmp_path):
    answer = [reply(("query_metric", DAILY), text="Looking."), reply(text="Done.")]
    script = [*answer, reply(("query_metric", BY_DEPT)), reply(text="Done."),
              reply(("query_metric", BY_LINE)), reply(text="Done."), reply(("refuse", {"reason": "No ranking."}))]
    model = ScriptedChatModel(script=script)
    spend = demo.Spend()
    assert demo.record(model, con, tmp_path, callbacks=[spend]) == 0
    assert model.script == [] and len(model.received) == 7  # one run per question, no retry

    index = json.loads((tmp_path / "index.json").read_text())
    assert index == [{"file": f"{n}.json", "question": q} for n, q in enumerate(demo.SAMPLE_QUESTIONS, 1)]
    first = json.loads((tmp_path / "1.json").read_text())
    assert first["question"] == demo.SAMPLE_QUESTIONS[0] and first["text"] == "Looking.Done."
    assert first["request"] == DAILY and first["refusal"] is None
    fourth = json.loads((tmp_path / "4.json").read_text())
    assert fourth["refusal"] == "No ranking." and fourth["rows"] is None
    catalog = load_catalog(con)
    for n in range(1, 5):
        check_sample(tmp_path / f"{n}.json", con, catalog)
    first["rows"][0][1] += 1  # the check is not vacuous: one changed value fails it
    (tmp_path / "1.json").write_text(json.dumps(first))
    with pytest.raises(AssertionError):
        check_sample(tmp_path / "1.json", con, catalog)
    assert spend.usage["input_tokens"] == 70 and spend.usage["output_tokens"] == 35


def test_record_skips_a_run_without_an_answer_and_fails(con, tmp_path):
    script = [reply(text="No tool call."), *[reply(("refuse", {"reason": "no"}))] * 3]
    assert demo.record(ScriptedChatModel(script=script), con, tmp_path) == 1
    assert not (tmp_path / "1.json").exists()
    assert [entry["file"] for entry in json.loads((tmp_path / "index.json").read_text())] == ["2.json", "3.json", "4.json"]


def test_record_refuses_to_run_without_a_key(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(demo, "SAMPLES", tmp_path / "samples")
    assert demo.main(["record", "--eur-per-usd", "0.9"]) == 1
    assert "ANTHROPIC_API_KEY" in capsys.readouterr().err
    assert not (tmp_path / "samples").exists()
