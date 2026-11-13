import json
import os
import subprocess
import sys
from datetime import UTC, datetime

import pytest
from fakes import ScriptedChatModel, reply
from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage

from agentic_ems.agent import (
    MODEL,
    ModelMismatch,
    make_model,
    render_rows,
    run_agent,
    semantic_arm,
    tracing_callbacks,
)
from agentic_ems.benchmark.sql_arm import sql_arm
from agentic_ems.semantic import catalogue_text, load_catalog, meter_tree_text, query

PERIOD = {"start": "2026-10-19", "end": "2026-11-01"}
GOOD = {"metric": "consumption_kwh", "breakdown": "department"} | PERIOD
BAD = {"metric": "consumption_kwh", "meters": ["dept_a", "line_a1"]} | PERIOD  # an ancestor with its child
Q = "How much energy did each department use?"


@pytest.fixture(scope="module")
def catalog(con):
    return load_catalog(con)


@pytest.fixture(scope="module")
def sem(con, catalog):
    return semantic_arm(con, catalog)


@pytest.fixture(scope="module")
def sql(con, catalog):
    return sql_arm(con, "Schema text.")


def run(arm, *script, **kwargs):
    model = ScriptedChatModel(script=list(script))
    return run_agent(model, arm, Q, **kwargs), model


def tool_messages(model):
    """The tool messages the model saw on its last call."""
    return [m for m in model.received[-1] if isinstance(m, ToolMessage)]


def test_one_query_then_text_ends_with_rows(sem, con, catalog):
    record, model = run(sem, reply(("query_metric", GOOD)), reply(text="Done."))
    expected = query(GOOD, con, catalog)
    assert record.final == "rows" and record.cause is None
    assert record.columns == expected.columns and record.rows == expected.rows
    assert record.calls[0]["sql"] == expected.sql
    assert len(model.received) == 2


def test_compile_error_is_returned_and_model_can_retry(sem):
    record, model = run(sem, reply(("query_metric", BAD)), reply(("query_metric", GOOD)), reply(text="Done."))
    first = model.received[1][-1]
    assert isinstance(first, ToolMessage) and first.status == "error" and "ancestor" in first.content
    assert [c["ok"] for c in record.calls] == [False, True]
    assert record.final == "rows" and len(record.rows) == 4


def test_malformed_tool_input_is_an_error_result(sem):
    record, model = run(sem, reply(("query_metric", PERIOD)), reply(text="I could not."))
    assert record.calls[0]["ok"] is False and "metric" in record.calls[0]["content"]
    assert len(model.received) == 2  # the loop continued after the error
    assert record.final == "none" and record.cause == "no_tool_call"


def test_refuse_ends_the_run(sem):
    record, model = run(sem, reply(("refuse", {"reason": "no such department"})), reply(text="unused"))
    assert record.final == "refusal" and record.refusal_reason == "no such department"
    assert record.rows is None and len(model.received) == 1


def test_fifth_tool_call_is_not_executed(sem):
    record, _ = run(sem, reply(*[("query_metric", BAD)] * 4, ("query_metric", GOOD)), reply(text="No luck."))
    assert len(record.calls) == 4 and record.final == "none" and record.cause == "max_tool_calls"

    record, _ = run(sem, reply(("query_metric", GOOD), *[("query_metric", BAD)] * 4), reply(text="Four rows."))
    assert len(record.calls) == 4 and record.final == "rows" and len(record.rows) == 4


def test_the_limit_is_followed_by_one_turn_without_tools(sem):
    record, model = run(sem, *[reply(("query_metric", GOOD))] * 4, reply(text="Summed up."))
    assert record.messages[-1]["content"] == "Summed up." and record.final == "rows"
    assert [k.get("tool_choice") for k in model.call_kwargs] == [None] * 4 + [{"type": "none"}]
    assert model.call_kwargs[-1]["tools"] == sem.tools  # the history holds tool calls, so the tools stay declared


def test_calls_in_one_message_run_in_order_and_count(sem):
    meters = [{"metric": "consumption_kwh", "node": m} | PERIOD for m in ("dept_a", "dept_b", "dept_c", "dept_d")]
    record, model = run(sem, reply(*[("query_metric", m) for m in meters[:3]]),
                        reply(("query_metric", meters[3]), ("query_metric", GOOD)), reply(text="Done."))
    assert [c["args"] for c in record.calls] == meters
    assert len(model.received) == 3 and tool_messages(model)[-1].status == "error"  # the fifth: answered, not run
    assert record.final == "rows" and record.rows == record.successful_results[-1][1]
    assert len(record.rows) == 1  # dept_d's total, not the breakdown that came fifth


def test_refuse_after_a_query_in_the_same_message(sem):
    record, _ = run(sem, reply(("query_metric", GOOD), ("refuse", {"reason": "not sure"})))
    assert record.final == "refusal" and record.refusal_reason == "not sure"
    assert len(record.successful_results) == 1


def test_max_tokens_after_rows_is_still_no_answer(sem):
    record, _ = run(sem, reply(("query_metric", GOOD)), reply(text="The answer is", stop_reason="max_tokens"))
    assert record.final == "none" and record.cause == "max_tokens" and record.rows is None
    assert len(record.successful_results) == 1


def test_last_successful_call_is_the_answer(sem, con, catalog):
    second = {"metric": "cost_eur", "breakdown": "department"} | PERIOD
    record, _ = run(sem, reply(("query_metric", GOOD)), reply(("query_metric", second)), reply(text="Done."))
    assert record.rows == query(second, con, catalog).rows
    assert [rows for _, rows in record.successful_results] == [query(GOOD, con, catalog).rows, record.rows]


def test_provider_refusal_is_no_answer(sem):
    record, model = run(sem, reply(("query_metric", GOOD), stop_reason="refusal"), reply(text="unused"))
    assert record.final == "none" and record.cause == "provider_refusal"
    assert record.calls == [] and len(model.received) == 1


def test_max_tokens_is_no_answer(sem):
    record, _ = run(sem, reply(("query_metric", GOOD), stop_reason="max_tokens"))
    assert record.final == "none" and record.cause == "max_tokens" and record.calls == []


def test_text_without_tool_call_is_no_answer(sem):
    record, _ = run(sem, reply(text="Department A used the most."))
    assert record.final == "none" and record.cause == "no_tool_call"


def test_wrong_served_model_raises(sem):
    with pytest.raises(ModelMismatch):
        run(sem, reply(text="Hi.", model="claude-opus-5"))
    with pytest.raises(ModelMismatch):  # a dated id of a different model
        run(sem, reply(text="Hi.", model="claude-opus-5-20260101"), requested_model="claude-opus-5-5")
    record, _ = run(sem, reply(text="Hi.", model=f"{MODEL}-20261101"))
    assert record.served_model == f"{MODEL}-20261101"


def test_usage_is_summed_from_raw_usage_dict(sem):
    usage = {"input_tokens": 7, "cache_creation_input_tokens": 1000, "cache_read_input_tokens": 0, "output_tokens": 40}
    cached = {"input_tokens": 9, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 1000, "output_tokens": 3}
    record, _ = run(sem, reply(("query_metric", GOOD), usage=usage), reply(text="Done.", usage=cached))
    assert record.usage == {
        "input_tokens": 16, "cache_creation_input_tokens": 1000, "cache_read_input_tokens": 1000, "output_tokens": 43,
    }


def test_system_has_one_cache_breakpoint_and_no_tool_choice(sem, sql, monkeypatch):
    for arm in (sem, sql):
        _, model = run(arm, reply(text="Hi."))
        system = model.received[0][0]
        assert system.type == "system" and system.content == arm.system
        assert [i for i, b in enumerate(arm.system) if "cache_control" in b] == [len(arm.system) - 1]
        assert isinstance(model.received[0][1], HumanMessage) and model.received[0][1].content == Q
        assert "tool_choice" not in model.call_kwargs[0] and model.call_kwargs[0]["tools"] == arm.tools

    # The real client keeps the breakpoint and sends no tool_choice.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    bound = make_model(effort="high", max_tokens=4000).bind_tools(sem.tools)
    payload = bound.bound._get_request_payload([SystemMessage(sem.system), HumanMessage(Q)], **bound.kwargs)
    assert payload["system"][-1]["cache_control"] == {"type": "ephemeral"}
    assert "tool_choice" not in payload and [t["name"] for t in payload["tools"]] == ["query_metric", "refuse"]
    closer = make_model(effort="high", max_tokens=4000).bind_tools(sem.tools, tool_choice={"type": "none"})
    payload = closer.bound._get_request_payload([SystemMessage(sem.system), HumanMessage(Q)], **closer.kwargs)
    assert payload["tool_choice"] == {"type": "none"} and len(payload["tools"]) == 2  # the closing turn's request


def test_arms_differ_only_in_context_and_exec_tool(sem, sql, catalog):
    *sem_fixed, sem_context = sem.system
    *sql_fixed, sql_context = sql.system
    assert json.dumps(sem_fixed).replace("query_metric", "run_sql") == json.dumps(sql_fixed)
    assert sem.tools[1] == sql.tools[1] and sem.tools[1]["name"] == "refuse"
    assert (sem.exec_tool, sql.exec_tool) == ("query_metric", "run_sql")
    assert sem_context["text"] == catalogue_text(catalog) + "\n\n" + meter_tree_text(catalog)
    assert sql_context["text"] == "Schema text."  # docs/schema.md carries the meter tree itself
    assert {k: v for k, v in sem_context.items() if k != "text"} == {k: v for k, v in sql_context.items() if k != "text"}


def test_both_arms_render_rows_identically(sem, sql, con, catalog):
    result = query(GOOD, con, catalog)
    scope, start, end = result.params
    # The compiled query with its parameters written in, so the SQL arm returns the very same rows.
    literal = result.sql.replace("IN ?", "IN (" + ", ".join(f"'{m}'" for m in scope) + ")", 1)
    literal = literal.replace("?", f"DATE '{start}'", 1).replace("?", f"DATE '{end}'", 1)
    from_sem, from_sql = sem.execute(GOOD), sql.execute({"sql": literal})
    assert from_sem.rows == from_sql.rows and from_sem.content == from_sql.content

    many = sql.execute({"sql": "SELECT range AS n FROM range(51)"})
    lines = many.content.splitlines()
    assert many.content == render_rows(["n"], [(i,) for i in range(51)])
    assert lines[0] == "n" and lines[1:51] == [str(i) for i in range(50)]
    assert len(lines) == 52 and "51" in lines[-1]
    assert len(many.rows) == 51  # the record keeps every row; only the model's text is cut


def test_semantic_arm_never_exposes_sql_tool(sem):
    assert [t["name"] for t in sem.tools] == ["query_metric", "refuse"]
    assert "sql" not in json.dumps(sem.tools).lower()
    _, model = run(sem, reply(("query_metric", GOOD)), reply(text="Done."))
    assert "SELECT" not in tool_messages(model)[0].content


def test_make_model_settings(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    model = make_model(effort="high", max_tokens=4000)
    assert model.model == MODEL and model.max_retries == 0 and model.temperature is None
    thinking = {"type": "adaptive", "display": "summarized"}
    assert model.thinking == thinking and model.max_tokens == 4000 and model.effort == "high"
    payload = model._get_request_payload([HumanMessage("Hi")])
    assert payload["output_config"] == {"effort": "high"} and payload["thinking"] == thinking
    assert payload["max_tokens"] == 4000 and "temperature" not in payload


def test_no_tracing_without_keys(monkeypatch):
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    assert tracing_callbacks() == []
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-test")
    assert tracing_callbacks() == []

    # A fresh interpreter, so nothing this session imported can hide a load of the Langfuse client.
    env = {k: v for k, v in os.environ.items() if not k.startswith("LANGFUSE_")}
    script = (
        "import sys; from agentic_ems.agent import tracing_callbacks; "
        "assert tracing_callbacks() == []; assert 'langfuse.langchain' not in sys.modules, 'loaded'"
    )
    subprocess.run([sys.executable, "-c", script], env=env, check=True)


def test_messages_are_json_serialisable(sem):
    record, _ = run(sem, reply(("query_metric", BAD)), reply(("query_metric", GOOD)), reply(text="Done."))
    roles = [m["role"] for m in json.loads(json.dumps(record.messages))]
    assert roles == ["system", "human", "ai", "tool", "ai", "tool", "ai"]
    json.dumps(record.calls)

    # A real response: content is a block list, and provider metadata may hold types JSON lacks.
    blocks = reply(("query_metric", GOOD))
    blocks.content = [
        {"type": "thinking", "thinking": "One query by department.", "signature": "sig"},
        {"type": "text", "text": "Querying."},
        {"type": "tool_use", "id": blocks.tool_calls[0]["id"], "name": "query_metric", "input": GOOD},
    ]
    blocks.response_metadata["created"] = datetime(2026, 11, 11, 9, 30, tzinfo=UTC)
    record, _ = run(sem, blocks, reply(text="Done."))
    first = json.loads(json.dumps(record.messages))[2]
    assert [b["type"] for b in first["content"]] == ["thinking", "text", "tool_use"]
    assert first["response_metadata"]["created"] == "2026-11-11 09:30:00+00:00"


def test_on_text_streams_the_reply_text(sem):
    texts: list[str] = []
    record, _ = run(sem, reply(("query_metric", GOOD), text="Looking."), reply(text="Done."), on_text=texts.append)
    assert record.final == "rows" and len(record.rows) == 4  # the query ran in LangGraph's worker thread
    assert "".join(texts) == "Looking.\n\nDone."  # the turns are separated


@pytest.mark.parametrize(
    "script",
    [
        [reply(("refuse", {"reason": "no data"}), ("query_metric", GOOD))],  # a call after refuse
        [reply(("query_metric", GOOD), stop_reason="refusal")],  # provider refusal with calls present
        [reply(("query_metric", GOOD), stop_reason="max_tokens")],  # cut off with calls present
    ],
)
def test_calls_not_run_end_the_run(sem, script):
    # A call is left without a result only when the run ends there, so no model call follows.
    # (Calls past the limit get an error result: the fake asserts every call has one before each model call.)
    model = ScriptedChatModel(script=[*script, reply(text="never sent")])
    record = run_agent(model, sem, Q)
    assert len(model.received) == len(script) and len(model.script) == 1
    assert record.final in ("none", "refusal")


def test_refuse_without_reason_is_a_refusal(sem):
    record, _ = run(sem, reply(("refuse", {})))
    assert record.final == "refusal" and record.refusal_reason == ""


def test_instructions_state_the_loop_limit(con, catalog, sem):
    assert "You have 4 tool calls in total." in sem.system[0]["text"]
    three = semantic_arm(con, catalog, max_tool_calls=3)
    assert "You have 3 tool calls in total." in three.system[0]["text"]
    record = run_agent(ScriptedChatModel(script=[reply(*[("query_metric", BAD)] * 4), reply(text="None.")]), three, Q)
    assert len(record.calls) == 3 and record.cause == "max_tool_calls"
