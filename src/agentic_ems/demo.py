"""The local demo: a server that streams the semantic arm's answer, and the recorder of the saved samples.

Run with: uv run python -m agentic_ems.demo serve   (free text, on the reader's own ANTHROPIC_API_KEY)
          uv run python -m agentic_ems.demo record --eur-per-usd RATE   (paid: one run per sample question)
"""

import argparse
import asyncio
import itertools
import json
import os
import sys
import threading
from collections.abc import AsyncIterator, Callable
from pathlib import Path

import uvicorn
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.language_models import BaseChatModel
from langchain_core.outputs import ChatGeneration, LLMResult
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from agentic_ems.agent import USAGE_KEYS, RunRecord, make_model, run_agent, semantic_arm
from agentic_ems.benchmark.run import cost_eur
from agentic_ems.semantic import DEFAULT_DB, load_catalog, open_db

ROOT = Path(__file__).resolve().parents[2]
DIST = ROOT / "frontend" / "dist"
SAMPLES = ROOT / "frontend" / "public" / "samples"
HOST, PORT = "127.0.0.1", 8000
MAX_QUESTION_CHARS = 500
MAX_BODY_BYTES = 8192  # a 500-character question fits many times over
EFFORT, MAX_TOKENS = "medium", 16000
KEY = "ANTHROPIC_API_KEY"
SAMPLE_QUESTIONS = (
    "How much electricity did the whole plant use each day in March 2026?",
    "Break down Stamping by line for May 2026.",
    (
        "Welding line 1 and Painting line 2 share a transformer. "
        "What was their highest combined load in the week of 9 March 2026?"
    ),
    "Which five machines used the most energy last quarter?",
)


def _demo_model() -> BaseChatModel:
    return make_model(effort=EFFORT, max_tokens=MAX_TOKENS)


def _answer(record: RunRecord) -> dict:
    """The parts of a run the page shows besides its text; a refusal has no request and no rows."""
    if record.final != "rows":
        return {"request": None, "sql": None, "columns": None, "rows": None, "refusal": record.refusal_reason}
    call = next(c for c in reversed(record.calls) if c["name"] == "query_metric" and c["ok"])
    # Several requests: the rows are then only the last request's, and the page says so.
    requests = sum(1 for c in record.calls if c["name"] == "query_metric" and c["ok"])
    return {"request": call["args"], "sql": call["sql"], "columns": record.columns, "rows": record.rows,
            "refusal": None, "requests": requests}


def _event(name: str, data: dict) -> str:
    return f"event: {name}\ndata: {json.dumps(data)}\n\n"  # json.dumps escapes newlines: one data line


class Disconnected(Exception):
    """The client went away, so the run stops."""


class StopOnDisconnect(BaseCallbackHandler):
    """Stops a run whose client went away: before its next model call, and at the next token of the one in flight."""

    raise_error = True  # so the error raised below stops the run instead of being logged and skipped

    def __init__(self, stop: threading.Event) -> None:
        self.stop = stop

    def on_chat_model_start(self, serialized: dict, messages: list, **kwargs) -> None:
        if self.stop.is_set():
            raise Disconnected

    def on_llm_new_token(self, token: object, **kwargs) -> None:
        if self.stop.is_set():
            raise Disconnected


def build_app(con, model_factory: Callable[[], BaseChatModel] = _demo_model) -> Starlette:
    with con.cursor() as cursor:
        catalog = load_catalog(cursor)
    arm = semantic_arm(con, catalog, say_today=True)  # shared and read-only; each query takes its own cursor
    running = threading.Lock()  # one run at a time: held from the run's start until its thread ends

    async def health(request: Request) -> JSONResponse:
        return JSONResponse({"ok": True, "has_key": bool(os.environ.get(KEY))})

    async def events(question: object) -> AsyncIterator[str]:
        # Every check comes before any model call; the question itself is never logged.
        if not isinstance(question, str) or not question.strip():
            yield _event("error", {"message": "Ask a question."})
        elif len(question) > MAX_QUESTION_CHARS:
            yield _event("error", {"message": f"The question is longer than {MAX_QUESTION_CHARS} characters."})
        elif not os.environ.get(KEY):
            yield _event("error", {"message": f"Set {KEY} in the server's environment to ask your own question."})
        elif not running.acquire(blocking=False):
            yield _event("error", {"message": "The server is busy with another question; ask again when it ends."})
        else:
            async for chunk in _run(question):
                yield chunk
        yield _event("done", {})

    async def _run(question: str) -> AsyncIterator[str]:
        # run_agent is synchronous: it runs on its own thread and hands events to this loop through a queue.
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[str | None] = asyncio.Queue()
        stop = threading.Event()

        def put(item: str | None) -> None:
            if not stop.is_set():
                loop.call_soon_threadsafe(queue.put_nowait, item)

        def work() -> None:
            try:
                # Events are encoded here, so a value JSON cannot encode ends in the error below, not a dead stream.
                record = run_agent(model_factory(), arm, question, callbacks=[StopOnDisconnect(stop)],
                                   on_text=lambda delta: put(_event("text", {"delta": delta})))
                answer = _answer(record)
                if record.final == "rows":
                    if answer["requests"] > 1:
                        put(_event("note", {"requests": answer["requests"]}))
                    put(_event("request", {"request": answer["request"], "sql": answer["sql"]}))
                    put(_event("rows", {"columns": answer["columns"], "rows": answer["rows"]}))
                elif record.final == "refusal":
                    put(_event("refusal", {"reason": answer["refusal"]}))
                else:
                    put(_event("error", {"message": f"The agent ended without an answer ({record.cause})."}))
            except Exception as exc:  # noqa: BLE001 - only the type reaches the page: no traceback, key or path
                put(_event("error", {"message": f"The run failed ({type(exc).__name__})."}))
            finally:
                running.release()
                put(None)

        threading.Thread(target=work, name="demo-run", daemon=True).start()
        try:
            while (item := await queue.get()) is not None:
                yield item
        finally:
            stop.set()  # on a disconnect Starlette cancels this generator; StopOnDisconnect then ends the run

    async def ask(request: Request) -> Response:
        # A page on another origin can POST text/plain without a preflight; only JSON, which needs one, is read.
        if request.headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
            return JSONResponse({"error": "Send the question as application/json."}, status_code=415)
        raw = b""
        async for chunk in request.stream():  # counted here: a chunked body carries no Content-Length to check
            raw += chunk
            if len(raw) > MAX_BODY_BYTES:
                return JSONResponse({"error": "The request is too large."}, status_code=413)
        try:
            body = json.loads(raw)
        except ValueError:
            body = None
        question = body.get("question") if isinstance(body, dict) else None
        return StreamingResponse(events(question), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})

    routes: list[Route | Mount] = [Route("/api/health", health), Route("/api/ask", ask, methods=["POST"])]
    if DIST.is_dir():
        routes.append(Mount("/", StaticFiles(directory=DIST, html=True)))  # StaticFiles refuses paths outside DIST
    # The Host check stops DNS rebinding from making an outside page same-origin; it ignores the port.
    return Starlette(routes=routes, middleware=[Middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])])


class Spend(BaseCallbackHandler):
    """The tokens of every response, those of a run that later fails included."""

    def __init__(self) -> None:
        self.usage = dict.fromkeys(USAGE_KEYS, 0)

    def on_llm_end(self, response: LLMResult, **kwargs) -> None:
        for generation in itertools.chain.from_iterable(response.generations):
            if isinstance(generation, ChatGeneration):
                raw = generation.message.response_metadata.get("usage") or {}
                for key in USAGE_KEYS:
                    self.usage[key] += raw.get(key) or 0


def _text(record: RunRecord) -> str:
    """The model's text across the run, as the server streams it: one turn's text after another, "\n\n" between."""
    parts = []
    for message in record.messages:
        if message["role"] == "ai":
            content = message["content"]
            parts.append(content if isinstance(content, str) else
                         "".join(block.get("text", "") for block in content if block.get("type") == "text"))
    return "\n\n".join(part for part in parts if part)


def record(model: BaseChatModel, con, out_dir: Path, callbacks: list | None = None) -> int:
    """One run per sample question, no retry; a refusal is saved, a run without an answer is not."""
    with con.cursor() as cursor:
        catalog = load_catalog(cursor)
    arm = semantic_arm(con, catalog, say_today=True)
    out_dir.mkdir(parents=True, exist_ok=True)
    index, status = [], 0
    for n, question in enumerate(SAMPLE_QUESTIONS, 1):
        run = run_agent(model, arm, question, callbacks=callbacks)
        if run.final == "none":
            print(f"Sample {n} ended without an answer ({run.cause}) and was not saved.", file=sys.stderr)
            (out_dir / f"{n}.json").unlink(missing_ok=True)  # an earlier recording of it would no longer be current
            status = 1
            continue
        sample = {"question": question, "text": _text(run), **_answer(run)}
        (out_dir / f"{n}.json").write_text(json.dumps(sample, indent=1) + "\n")
        index.append({"file": f"{n}.json", "question": question})
    (out_dir / "index.json").write_text(json.dumps(index, indent=1) + "\n")
    return status


def main(argv: list[str] | None = None) -> int:
    # Before any LangChain call: no transcript leaves the machine through LangSmith.
    os.environ["LANGSMITH_TRACING"] = "false"
    os.environ["LANGCHAIN_TRACING_V2"] = "false"  # the legacy name of the same switch
    parser = argparse.ArgumentParser(description="The local demo server and the sample recorder.")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("serve", help=f"serve the demo on http://{HOST}:{PORT}")
    rec = commands.add_parser("record", help="paid: run the sample questions and write frontend/public/samples/")
    rec.add_argument("--eur-per-usd", type=float, required=True)
    args = parser.parse_args(argv)
    if args.command == "record" and not os.environ.get(KEY):
        print(f"record makes paid API calls: set {KEY} first.", file=sys.stderr)
        return 1
    if args.command == "record" and args.eur_per_usd <= 0:
        parser.error("--eur-per-usd must be above 0")
    try:
        con = open_db(DEFAULT_DB)
    except FileNotFoundError as exc:
        print(exc, file=sys.stderr)
        return 1
    if args.command == "serve":
        uvicorn.run(build_app(con), host=HOST, port=PORT)
        return 0
    spend = Spend()
    try:
        return record(_demo_model(), con, SAMPLES, callbacks=[spend])
    finally:
        tokens = ", ".join(f"{key} {value}" for key, value in spend.usage.items())
        print(f"Spent {cost_eur(spend.usage, args.eur_per_usd):.4f} EUR: {tokens}", file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
