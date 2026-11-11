"""A chat model that plays back scripted replies, so the agent loop runs offline."""

from itertools import count
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field

from agentic_ems.agent import MODEL

IDS = count()  # tool_use ids are unique across every scripted reply, as the API makes them
USAGE = {"input_tokens": 10, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0, "output_tokens": 5}


def reply(*calls: tuple[str, dict], text: str = "", stop_reason: str | None = None, model: str = MODEL,
          usage: dict | None = None) -> AIMessage:
    """One scripted response; each call is (tool name, input)."""
    return AIMessage(
        content=text,
        tool_calls=[{"name": name, "args": args, "id": f"toolu_{next(IDS)}", "type": "tool_call"} for name, args in calls],
        response_metadata={
            "model": model,
            "stop_reason": stop_reason or ("tool_use" if calls else "end_turn"),
            "usage": usage or USAGE,
        },
    )


class ScriptedChatModel(BaseChatModel):
    script: list[AIMessage]
    received: list[list[BaseMessage]] = Field(default_factory=list)
    tools: list = Field(default_factory=list)
    call_kwargs: list[dict] = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools, **kwargs: Any):
        self.tools = list(tools)
        return self.bind(tools=self.tools, **kwargs)

    def _generate(self, messages: list[BaseMessage], stop=None, run_manager=None, **kwargs: Any) -> ChatResult:
        # As the API requires: every tool call of the previous response has its result before the next call.
        last_ai = max((i for i, m in enumerate(messages) if isinstance(m, AIMessage)), default=None)
        if last_ai is not None:
            answered = {m.tool_call_id for m in messages[last_ai + 1:] if isinstance(m, ToolMessage)}
            missing = [c["id"] for c in messages[last_ai].tool_calls if c["id"] not in answered]  # type: ignore[attr-defined]
            assert not missing, f"tool calls without a result: {missing}"
        self.received.append(list(messages))
        self.call_kwargs.append(kwargs)
        return ChatResult(generations=[ChatGeneration(message=self.script.pop(0))])
