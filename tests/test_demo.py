import json
import threading
import time
from collections.abc import Callable

import pytest
from fakes import ScriptedChatModel, reply
from starlette.testclient import TestClient

from agentic_ems import demo
from agentic_ems.agent import run_agent, semantic_arm
from agentic_ems.semantic import load_catalog, open_db, query

PERIOD = {"start": "2026-10-20", "end": "2026-10-23"}
DAILY = {"metric": "consumption_kwh", "node": "site", "grain": "day"} | PERIOD
BY_DEPT = {"metric": "consumption_kwh", "breakdown": "department"} | PERIOD
BY_LINE = {"metric": "peak_kw", "meters": ["line_a1", "line_b2"]} | PERIOD
Q = "How much electricity did the whole plant use each day?"
LOCAL = "http://127.0.0.1:8000"


def until(done: Callable[[], bool], limit_s: float = 10.0) -> None:
    """Polls on the monotonic clock: on this Python build timed lock waits end on the wall clock, which the host steps."""
    deadline = time.monotonic() + limit_s
    while not done():
        assert time.monotonic() < deadline, f"not done within {limit_s:g} seconds"
        time.sleep(0.01)


def events(body: str) -> list[tuple[str, dict]]:
    out = []
    for block in body.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.split("\n"))
        out.append((lines["event"], json.loads(lines["data"])))
    return out


def client(app, base_url=LOCAL) -> TestClient:
    return TestClient(app, base_url=base_url)


def post(app, question) -> list[tuple[str, dict]]:
    response = client(app).post("/api/ask", json={"question": question})
    assert response.headers["content-type"].startswith("text/event-stream")
    return events(response.text)


def ask(con, model, question, monkeypatch, key="test-key"):
    if key:
        monkeypatch.setenv("ANTHROPIC_API_KEY", key)
    else:
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    return post(demo.build_app(con, lambda: model), question)


def streamed_text(got) -> str:
    return "".join(data["delta"] for name, data in got if name == "text")


def test_answered_question_streams_text_then_request_rows_done(con, monkeypatch):
    model = ScriptedChatModel(script=[reply(("query_metric", DAILY), text="Looking."), reply(text="Done.")])
    got = ask(con, model, Q, monkeypatch)
    names = [name for name, _ in got]
    assert names[-3:] == ["request", "rows", "done"] and set(names[:-3]) == {"text"}
    assert streamed_text(got) == "Looking.\n\nDone."  # separate model turns, separated
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
    got = post(demo.build_app(con, broken), Q)
    assert [name for name, _ in got] == ["error", "done"]
    message = got[0][1]["message"]
    assert "\n" not in message and "secret" not in message and "/" not in message


def test_health_reports_only_whether_a_key_is_present(con, monkeypatch):
    local = client(demo.build_app(con, lambda: None))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-very-secret")
    assert local.get("/api/health").json() == {"ok": True, "has_key": True}
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    assert local.get("/api/health").json() == {"ok": True, "has_key": False}


def test_server_never_exposes_run_sql(con, monkeypatch):
    model = ScriptedChatModel(script=[reply(("run_sql", {"sql": "SELECT 1"})), reply(text="Done.")])
    got = ask(con, model, Q, monkeypatch)
    assert [tool["name"] for tool in model.tools] == ["query_metric", "refuse"]
    assert "rows" not in [name for name, _ in got]  # the call was answered with an error, not run
    assert client(demo.build_app(con, lambda: None)).post("/api/run_sql", json={}).status_code in (404, 405)


def test_a_cross_site_simple_request_makes_no_model_call(con, monkeypatch):
    # A page on another origin can send text/plain without a preflight; it must not start a paid run.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    model = ScriptedChatModel(script=[reply(text="never sent")])
    response = client(demo.build_app(con, lambda: model)).post(
        "/api/ask", content=json.dumps({"question": Q}), headers={"content-type": "text/plain"})
    assert response.status_code == 415 and "application/json" in response.json()["error"]
    assert model.received == []


@pytest.mark.parametrize("base_url", ["http://evil.example:8000", "http://testserver"])
def test_a_request_for_another_host_is_refused(con, monkeypatch, base_url):
    # DNS rebinding sends the attacker's host name in the Host header.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    model = ScriptedChatModel(script=[reply(text="never sent")])
    app = demo.build_app(con, lambda: model)
    assert client(app, base_url).post("/api/ask", json={"question": Q}).status_code == 400
    assert client(app, base_url).get("/api/health").status_code == 400
    assert model.received == []


def test_localhost_is_served(con):
    assert client(demo.build_app(con, lambda: None), "http://localhost:8000").get("/api/health").status_code == 200


def test_a_value_json_cannot_encode_is_an_error_then_done(con, monkeypatch):
    real = demo._answer
    monkeypatch.setattr(demo, "_answer", lambda record: real(record) | {"rows": [[object()]]})
    model = ScriptedChatModel(script=[reply(("query_metric", DAILY)), reply(text="Done.")])
    got = ask(con, model, Q, monkeypatch)
    assert [name for name, _ in got][-2:] == ["error", "done"] and "rows" not in [name for name, _ in got]


def test_a_second_question_while_one_runs_is_busy(con, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    entered, release, built = threading.Event(), threading.Event(), []
    model = ScriptedChatModel(script=[reply(("refuse", {"reason": "no"})), reply(("refuse", {"reason": "again"}))])

    def factory():
        built.append(1)
        entered.set()
        until(release.is_set)
        return model

    app = demo.build_app(con, factory)
    first: list = []
    thread = threading.Thread(target=lambda: first.append(post(app, Q)))
    thread.start()
    try:
        until(entered.is_set)
        busy = post(app, Q)
    finally:
        release.set()
        until(lambda: not thread.is_alive())
    assert [name for name, _ in busy] == ["error", "done"] and "busy" in busy[0][1]["message"]
    assert first[0][-2] == ("refusal", {"reason": "no"}) and len(built) == 1
    assert post(app, Q)[-2] == ("refusal", {"reason": "again"})  # the run's end frees the server


def test_a_disconnect_stops_the_next_model_call_and_the_one_in_flight(con):
    stop = threading.Event()
    gone = demo.StopOnDisconnect(stop)
    gone.on_llm_new_token("still here")
    model = ScriptedChatModel(script=[reply(("query_metric", DAILY), text="Looking."), reply(text="never sent")])
    with pytest.raises(demo.Disconnected):
        run_agent(model, semantic_arm(con, load_catalog(con)), Q, callbacks=[gone], on_text=lambda _: stop.set())
    assert len(model.received) == 1
    with pytest.raises(demo.Disconnected):
        gone.on_llm_new_token("cut")


def check_sample(path, con, catalog) -> None:
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
    # DuckDB sums in parallel, so the last digits of a float can differ between two runs of one query.
    assert sample["rows"] == [[pytest.approx(cell, rel=1e-9) if isinstance(cell, float) else cell for cell in row]
                              for row in json.loads(json.dumps(result.rows))]


INDEX = demo.SAMPLES / "index.json"


# No index gives zero cases, which pytest reports as a skip with its reason.
@pytest.mark.parametrize("entry", json.loads(INDEX.read_text()) if INDEX.exists() else [], ids=lambda e: e["file"])
def test_saved_sample_replays_on_the_full_database(entry, full_db_path):
    con = open_db(full_db_path)
    check_sample(demo.SAMPLES / entry["file"], con, load_catalog(con))


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
    assert first["question"] == demo.SAMPLE_QUESTIONS[0] and first["text"] == "Looking.\n\nDone."
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


def test_saved_text_is_what_the_server_streams(con, tmp_path, monkeypatch):
    turns = [reply(("query_metric", DAILY), text="One."), reply(("query_metric", BY_DEPT)),
             reply(("query_metric", BY_LINE), text="Two."), reply(text="Three.")]
    refusals = [reply(("refuse", {"reason": "no"}))] * 3
    demo.record(ScriptedChatModel(script=[*turns, *refusals]), con, tmp_path)
    got = ask(con, ScriptedChatModel(script=turns), Q, monkeypatch)
    assert streamed_text(got) == json.loads((tmp_path / "1.json").read_text())["text"] == "One.\n\nTwo.\n\nThree."


def test_record_skips_a_run_without_an_answer_and_fails(con, tmp_path):
    (tmp_path / "1.json").write_text("an earlier recording")
    script = [reply(text="No tool call."), *[reply(("refuse", {"reason": "no"}))] * 3]
    assert demo.record(ScriptedChatModel(script=script), con, tmp_path) == 1
    assert not (tmp_path / "1.json").exists()
    assert [entry["file"] for entry in json.loads((tmp_path / "index.json").read_text())] == ["2.json", "3.json", "4.json"]


def test_record_refuses_to_run_without_a_key(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("LANGSMITH_TRACING", "false")  # main sets both; monkeypatch restores them afterwards
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "false")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(demo, "SAMPLES", tmp_path / "samples")
    assert demo.main(["record", "--eur-per-usd", "0.9"]) == 1
    assert "ANTHROPIC_API_KEY" in capsys.readouterr().err
    assert not (tmp_path / "samples").exists()


def test_an_oversized_body_is_refused_before_it_is_parsed(con, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    model = ScriptedChatModel(script=[reply(text="never sent")])
    response = client(demo.build_app(con, lambda: model)).post(
        "/api/ask", content=json.dumps({"question": "x" * demo.MAX_BODY_BYTES}),
        headers={"content-type": "application/json"})
    assert response.status_code == 413 and model.received == []
