"""One tool loop for both benchmark arms and the demo.

The arms differ only in their context and their execution tool; the graph, the limit of 4 tool
calls, the instructions and the way a run ends are the same code for both.
"""

import csv
import io
import json
import operator
import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated, Any, Literal, TypedDict

from langchain_anthropic import ChatAnthropic
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    AnyMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langgraph.graph import END, START, StateGraph

from agentic_ems.semantic import (
    Catalog,
    LimitError,
    QueryResult,
    RequestError,
    catalogue_text,
    meter_tree_text,
    query,
    tool_schema,
)

MODEL = "claude-opus-5-5"
MAX_TOOL_CALLS = 4
USAGE_KEYS = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens")
INSTRUCTIONS = (
    "You answer questions about the energy data of one manufacturing plant. "
    "You have {max_tool_calls} tool calls in total. The rows of your last successful {exec_tool} call are your answer. "
    "If the question cannot be answered from the data, call refuse with the reason instead of returning rows. "
    "Do not ask the user questions."
)
REFUSE_TOOL = {
    "name": "refuse",
    "description": "Refuse the question because it cannot be answered from the data, and say why.",
    "input_schema": {"type": "object", "properties": {"reason": {"type": "string"}}, "required": ["reason"]},
}


@dataclass
class ToolResult:
    ok: bool
    content: str
    columns: list[str] | None = None
    rows: list[tuple] | None = None
    sql: str | None = None


@dataclass
class Arm:
    name: str
    system: list[dict]
    tools: list[dict]
    exec_tool: str
    execute: Callable[[dict], ToolResult]
    max_tool_calls: int = MAX_TOOL_CALLS  # the count its instructions state


@dataclass
class RunRecord:
    arm: str
    question: str
    messages: list[dict]
    calls: list[dict]
    final: Literal["rows", "refusal", "none"]
    columns: list[str] | None
    rows: list[tuple] | None
    successful_results: list[tuple[list[str], list[tuple]]]
    refusal_reason: str | None
    cause: str | None  # set only when final == "none": "max_tool_calls", "max_tokens", "provider_refusal", "no_tool_call"
    usage: dict[str, int]
    served_model: str


class ModelMismatch(RuntimeError):
    """A response came from a model other than the one requested."""


def make_model(*, model: str = MODEL, effort: str, max_tokens: int) -> ChatAnthropic:
    # Everything that shapes the request is explicit, and retries are off so no call is hidden.
    # mypy knows these two fields only by their aliases, model_name and max_tokens_to_sample.
    return ChatAnthropic(  # type: ignore[call-arg]
        model=model,
        max_retries=0,
        max_tokens=max_tokens,
        effort=effort,  # type: ignore[arg-type]
        thinking={"type": "adaptive", "display": "summarized"},  # transcripts keep the reasoning summary
    )


def render_rows(columns: list[str], rows: list[tuple], max_rows: int = 50) -> str:
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(columns)
    writer.writerows(rows[:max_rows])
    shown = f", the first {max_rows} shown" if len(rows) > max_rows else ""
    return out.getvalue() + f"{len(rows)} rows in total{shown}"


def make_arm(name: str, exec_tool: dict, context: str, con, run: Callable[[Any, dict], QueryResult],
             today: str | None = None, max_tool_calls: int = MAX_TOOL_CALLS) -> Arm:
    """The parts both arms share: instructions, refuse tool, cache breakpoint, cursor and rendering."""
    instructions = INSTRUCTIONS.format(exec_tool=exec_tool["name"], max_tool_calls=max_tool_calls) + (f" Today is {today}." if today else "")
    system: list[dict] = [
        {"type": "text", "text": instructions},
        {"type": "text", "text": context, "cache_control": {"type": "ephemeral"}},
    ]

    def execute(args: dict) -> ToolResult:
        # A cursor per call: under graph.stream LangGraph runs the tool node in a worker thread.
        with con.cursor() as cursor:
            result = run(cursor, args)
        return ToolResult(True, render_rows(result.columns, result.rows), result.columns, result.rows, result.sql)

    return Arm(name, system, [exec_tool, REFUSE_TOOL], exec_tool["name"], execute, max_tool_calls)


def semantic_arm(con, catalog: Catalog, say_today: bool = False, max_tool_calls: int = MAX_TOOL_CALLS) -> Arm:
    tool = {
        "name": "query_metric",
        "description": "Run one metric request against the plant data and return its rows.",
        "input_schema": tool_schema(),
    }
    context = catalogue_text(catalog) + "\n\n" + meter_tree_text(catalog)
    today = catalog.end_date.isoformat() if say_today else None
    return make_arm("semantic", tool, context, con, lambda cursor, args: query(args, cursor, catalog), today,
                    max_tool_calls)


class State(TypedDict):
    messages: Annotated[list[AnyMessage], operator.add]
    calls: Annotated[list[dict], operator.add]  # one per executed tool call: name, args, result; its length is the count


def build_graph(model: BaseChatModel, arm: Arm):
    bound = model.bind_tools(arm.tools)
    # ChatAnthropic passes a dict through as the API's tool_choice; the base class types it as str.
    closer = model.bind_tools(arm.tools, tool_choice={"type": "none"})  # type: ignore[arg-type]
    system = SystemMessage(arm.system)  # type: ignore[arg-type]

    def llm_call(state: State) -> dict:
        return {"messages": [bound.invoke([system, *state["messages"]])]}

    def tool_node(state: State) -> dict:
        messages: list[ToolMessage] = []
        calls: list[dict] = []
        last = state["messages"][-1]
        assert isinstance(last, AIMessage)
        for call in last.tool_calls:
            if len(state["calls"]) + len(calls) == arm.max_tool_calls:
                # Not run and not counted, but answered: the closing turn needs a result for every call.
                messages.append(ToolMessage("Error: not run, the tool call limit is reached.",
                                            tool_call_id=call["id"], status="error"))
                continue
            if call["name"] == "refuse":
                result = ToolResult(True, "Refusal recorded.")
            elif call["name"] == arm.exec_tool:
                try:
                    result = arm.execute(call["args"])
                except (RequestError, LimitError) as exc:
                    result = ToolResult(False, f"Error: {exc}")
            else:
                result = ToolResult(False, f"Error: there is no tool named {call['name']!r}")
            calls.append({"name": call["name"], "args": call["args"], "result": result})
            messages.append(
                ToolMessage(result.content, tool_call_id=call["id"], status="success" if result.ok else "error")
            )
            if call["name"] == "refuse":
                break
        return {"messages": messages, "calls": calls}

    def after_llm(state: State) -> str:
        last = state["messages"][-1]
        if last.response_metadata.get("stop_reason") in ("refusal", "max_tokens"):
            return END
        return "tool_node" if isinstance(last, AIMessage) and last.tool_calls else END

    def closing(state: State) -> dict:
        # The limit is reached: one last turn with the tools switched off, so the run ends with text.
        return {"messages": [closer.invoke([system, *state["messages"]])]}

    def after_tools(state: State) -> str:
        if state["calls"][-1]["name"] == "refuse":
            return END
        return "closing" if len(state["calls"]) >= arm.max_tool_calls else "llm_call"

    graph = StateGraph(State)
    graph.add_node("llm_call", llm_call)
    graph.add_node("tool_node", tool_node)
    graph.add_edge(START, "llm_call")
    graph.add_conditional_edges("llm_call", after_llm, ["tool_node", END])
    graph.add_node("closing", closing)
    graph.add_conditional_edges("tool_node", after_tools, ["llm_call", "closing", END])
    graph.add_edge("closing", END)
    return graph.compile()


def _message_dict(message: BaseMessage) -> dict:
    out: dict[str, Any] = {"role": message.type, "content": message.content}
    if isinstance(message, AIMessage):
        out |= {"tool_calls": message.tool_calls, "response_metadata": message.response_metadata}
    if isinstance(message, ToolMessage):
        out |= {"tool_call_id": message.tool_call_id, "status": message.status}
    return json.loads(json.dumps(out, default=str))  # plain JSON types; provider extras such as datetimes become text


def run_agent(model: BaseChatModel, arm: Arm, question: str, *, requested_model: str = MODEL,
              callbacks: list | None = None, on_text: Callable[[str], None] | None = None) -> RunRecord:
    graph = build_graph(model, arm)
    start = {"messages": [HumanMessage(question)], "calls": []}
    config = {"callbacks": callbacks or []}
    if on_text is None:
        state = graph.invoke(start, config)  # type: ignore[arg-type]
    else:
        # The demo: stream the model's text as it arrives; "values" carries the final state.
        texted = None  # the id of the last message whose text was sent
        for mode, data in graph.stream(start, config, stream_mode=["messages", "values"]):  # type: ignore[arg-type]
            if mode == "values":
                state = data
            elif data[1].get("langgraph_node") in ("llm_call", "closing") and data[0].text:
                if texted is not None and data[0].id != texted:
                    on_text("\n\n")  # a new model turn: its text does not run into the previous one
                texted = data[0].id
                on_text(data[0].text)

    responses = [m for m in state["messages"] if isinstance(m, AIMessage)]
    served_model = responses[-1].response_metadata.get("model", "")
    usage = dict.fromkeys(USAGE_KEYS, 0)
    if on_text is None:
        for response in responses:
            served = response.response_metadata.get("model", "")
            # The API echoes the alias or a dated id of it.
            if re.sub(r"-\d{8}$", "", served) != requested_model:
                raise ModelMismatch(f"requested {requested_model}, served {served!r}")
            raw = response.response_metadata.get("usage") or {}
            for key in USAGE_KEYS:
                usage[key] += raw.get(key) or 0

    calls = state["calls"]
    successes = [c["result"] for c in calls if c["name"] == arm.exec_tool and c["result"].ok]
    stop_reason = responses[-1].response_metadata.get("stop_reason")
    final: Literal["rows", "refusal", "none"] = "none"
    cause = refusal_reason = None
    if stop_reason in ("refusal", "max_tokens"):  # the graph ends on such a response, so it is the last
        cause = "provider_refusal" if stop_reason == "refusal" else "max_tokens"
    elif calls and calls[-1]["name"] == "refuse":
        # A refuse call without a reason still ends the run as a refusal, with an empty reason.
        final, refusal_reason = "refusal", str(calls[-1]["args"].get("reason", ""))
    elif successes:
        final = "rows"
    else:
        cause = "max_tool_calls" if len(calls) >= arm.max_tool_calls else "no_tool_call"
    answer = successes[-1] if final == "rows" else None

    return RunRecord(
        arm=arm.name,
        question=question,
        messages=[{"role": "system", "content": arm.system}] + [_message_dict(m) for m in state["messages"]],
        calls=[
            {"name": c["name"], "args": c["args"], "ok": c["result"].ok, "content": c["result"].content,
             "sql": c["result"].sql}
            for c in calls
        ],
        final=final,
        columns=answer.columns if answer else None,
        rows=answer.rows if answer else None,
        successful_results=[(r.columns, r.rows) for r in successes],
        refusal_reason=refusal_reason,
        cause=cause,
        usage=usage,
        served_model=served_model,
    )


def tracing_callbacks() -> list:
    if not (os.environ.get("LANGFUSE_PUBLIC_KEY") and os.environ.get("LANGFUSE_SECRET_KEY")):
        return []
    # Imported only when tracing is configured, so a run without keys never loads the client.
    from langfuse.langchain import CallbackHandler

    return [CallbackHandler()]
