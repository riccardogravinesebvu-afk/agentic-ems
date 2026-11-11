# agentic-ems Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Answer natural-language questions on a synthetic plant's energy data through a semantic layer that cannot double count the meter hierarchy, and measure it against text-to-SQL on the same questions.

**Architecture:** A generator builds a DuckDB file from a seed. One file declares the metrics and hierarchy rules; a pure `compile` turns a structured request into SQL and `query` is the only entry point that executes it. One LangGraph tool loop serves two arms (structured request, or model-written SQL), the benchmark, and a localhost demo; a static page replays saved answers.

**Tech Stack:** Python 3.11+, uv, pytest 9.2.0, `duckdb` 2.0.0, `sqlglot` 30.31.0, `langgraph` 1.2.20, `langchain-anthropic` 1.7.10, `langfuse` 5.2.0, `mcp` 2.3.0 (brings Starlette and uvicorn), React 19 + shadcn/ui + Recharts built with Vite. Versions and reasons: `docs/stack.md`.

**Spec:** `docs/designs/agentic-ems.md`. Read it before any task; this plan fixes names and order, the spec fixes behaviour.

## Global Constraints

- Clean-room: read only files in this repository, installed skills, public package indexes and official docs. Never open another directory on this machine. No employer or customer names, no table or column names from any real system.
- Offline tests pass with `uv run pytest` and no API key. No test makes a network call.
- No paid API call without the owner's explicit approval. Benchmark calls (pilot included) need an approved `/eval-plan` spend table; before each paid gate, check whether `LANGFUSE_*` variables are set and have the owner confirm where traces will go. Benchmark budget 20 EUR in total, pilot included. Development calls have a separate cap the owner sets before the first one.
- Model ids, prices and request parameters come from the `claude-api` skill, loaded on the day the code is written. Never from memory. Forced `tool_choice` and sampling parameters are rejected by current models; set effort explicitly.
- For LangGraph, `langchain-anthropic`, Langfuse, `sqlglot`, `mcp` 2.x, DuckDB 2.0 settings and shadcn/ui install steps, read the official docs before writing code. use the official documentation sites, never memory.
- One commit per task, message says why, no co-author or tool trailers. This rule wins over any trailer instruction an agent's environment injects. Git identity is already set in the repository's local config; do not change it.
- Smallest version that does the job. No option, flag or abstraction the spec does not name. Runtime dependencies are only the ones in the Tech Stack line, declared explicitly in `pyproject.toml` when imported directly (`pydantic`, `langchain-core`, `starlette`, `uvicorn` included), with `uv.lock` committed. Development tools: `pytest`, `ruff`, `mypy`; `uv run ruff check` and `uv run mypy src` pass before every commit.
- Nothing is published, pushed or deployed. `BRIEF.md` and `CLEANROOM-PLAN.md` are never committed.
- English in code, docs and commit messages. Plain sentences, no em dashes.

## Review Focus

Inputs the spec implies but does not test. Each has a test in the task named.

1. The database file does not exist when `query`, the MCP server or the demo starts: one-line error naming the command that builds it (Task 4).
2. The model sends tool input that does not fit the schema (missing field, wrong type, unknown metric): returned to the model as a tool error, the loop continues, nothing crashes (Task 6).
3. `run_sql` receives something other than one read statement (`INSERT`, `SET lock_configuration = false`, `ATTACH`, `COPY ... TO`, two statements): each is an error result, and the database is still readable afterwards (Task 5).
4. The SQL arm returns rows the scorer did not expect: duplicate key rows, a text or NaN `value`, zero rows, 5,001 rows: outcome `other_wrong` or the row-limit error, never an exception (Task 10).
5. A period with start equal to or after end, or a free-text question that is empty or longer than the limit: rejected with a message before any SQL or API call (Tasks 2 and 14).

## File Structure

```
pyproject.toml, uv.lock
src/agentic_ems/
  generator.py        build the database from a seed
  semantic.py         metrics, rules, request type, validate, compile, open_db, query
  agent.py            the one LangGraph tool loop, the semantic arm, the model factory, optional tracing
  mcp_server.py       three tools over stdio
  benchmark/
    sql_arm.py        run_sql and the text-to-SQL arm
    questions.toml    question text, group, shape, source, mistake, rejected list
    reference_sql.toml  reference query and known wrong queries per question
    reference_py.py   second computation in plain Python
    verify.py         one command: rebuild, compute both, compare
    scorer.py         outcome of one run
    run.py            paid harness
    report.py         results file to tables and the README block
  demo.py             localhost server and the saved-answer recorder
frontend/             Vite + React + shadcn, one page; public/samples/*.json
tests/                one test file per module
docs/                 schema.md, golden.md, review-log.md, video-script.md
.github/workflows/    test.yml, pages.yml (manual trigger only)
```

The database is written to `data/plant.duckdb`, which is ignored by git.

---

### Task 1: Project scaffold and data generator

**Files:** Create `pyproject.toml`, `src/agentic_ems/__init__.py`, `src/agentic_ems/generator.py`, `tests/conftest.py`, `tests/test_generator.py`. Modify `.gitignore` (add `data/`, `.venv/`, `frontend/node_modules/`, `frontend/dist/`).

**Interfaces — Produces:**
- `generate(path: Path, seed: int = 20261111, start: date = date(2025, 11, 1), end: date = date(2026, 11, 1)) -> None`. Overwrites `path`. Closes its connection before returning.
- CLI: `uv run python -m agentic_ems.generator` writes `data/plant.duckdb`.
- Tables (all names final):
  - `meter(meter_id VARCHAR PRIMARY KEY, name VARCHAR, level VARCHAR, parent_id VARCHAR, kind VARCHAR)`. Consumption tree: `site`; departments `dept_a`..`dept_d`; lines `line_a1`.. (3, 3, 2, 2 per department); machines `mach_a1_1`.. (two or three per line, 26 in total). Machine names are a type and a number (`Press 1`, `Oven 2`); at least one type has machines on more than one line. `solar` (`generation`) and `battery` (`storage`) have NULL `level` and `parent_id`.
  - `reading(meter_id, ts_utc TIMESTAMP, kwh DOUBLE, local_start VARCHAR, hour_start VARCHAR, day_start VARCHAR, month_start VARCHAR, local_date DATE, tariff_band VARCHAR)`. The four `*_start` columns are ISO 8601 with the UTC offset in force at that bucket's own start instant, for example `2026-10-25T02:00:00+01:00`; a local day or month therefore has exactly one `day_start` or `month_start`, also across a clock change.
  - `tariff(tariff_band VARCHAR PRIMARY KEY, price_eur_per_kwh DOUBLE)`: `F1` 0.28, `F2` 0.24, `F3` 0.20. Bands: F1 Monday to Friday 08:00-19:00; F2 Monday to Friday 07:00-08:00 and 19:00-23:00, Saturday 07:00-23:00; F3 everything else. Local time.
  - `production_order(order_id INTEGER PRIMARY KEY, line_id VARCHAR, product VARCHAR, start_utc TIMESTAMP, end_utc TIMESTAMP, units INTEGER)`. No order runs on a Sunday (local time).
  - `dataset_info(seed INTEGER, timezone VARCHAR, first_date DATE, end_date DATE)`, one row; `end_date` is exclusive and is the demo's "today".
- `tests/conftest.py`, all session scope: `db_path`, a database for 2026-10-19 to 2026-11-01, which contains the autumn clock-change day; `con`, a read-only `duckdb.connect` on it (Task 4 replaces this with `open_db`); `full_db_path`, the default one-year database built once per test session, for the tests that need every question's period (Tasks 9, 10, 15).

**Approach:** `random.Random(seed)` in Python, `zoneinfo` for Europe/Rome, time columns computed once per interval and reused for every meter. Machine kWh has a floor of 0.5 per interval and is rounded to 3 decimals. Each parent draws one margin in [0.03, 0.08] and reads `sum(children) * (1 + margin)` in every interval. Lines run different shifts, so two lines do not peak in the same interval.

**Load realism (the profile must look like a plant, not like noise):** each machine has a standby load and a working load; it works only while an order runs on its line, with a short start-up ramp and a duty cycle with noise, so line kWh follows production and energy per unit is meaningful. Lines run one, two or three shifts on weekdays, some run Saturday morning, none on Sunday. Night and Sunday consumption is standby only. Two plant closures have no orders: 2025-12-24 to 2026-01-06 and 2026-08-08 to 2026-08-23. Department-level unmetered load (the 3 to 8 percent margin) covers lighting and auxiliaries and needs no extra modelling. Solar follows a daylight bell curve whose height and length change with the season, scaled per day by a random cloud factor, and is zero at night. The battery charges around midday when solar is high and discharges in the early evening. None of this may break an invariant above: the 0.5 kWh floor, the per-parent constant margin, 15-minute order boundaries. Rows go to a temporary CSV and are loaded with `read_csv`.

- [ ] **Step 1:** Write `tests/test_generator.py`:

```python
def test_same_seed_same_database(tmp_path):      # two runs over a 3-day span, identical sum(kwh), count(*) and md5 of ordered rows per table
def test_parent_margin_holds_every_interval(con): # for every parent and ts: 1.03 - 1e-3 <= parent / sum(children) <= 1.08 + 1e-3, and max - min of the ratio per parent < 2e-3
def test_autumn_day_has_100_intervals(con):       # local_date 2026-10-25, meter 'site': count == 100, 25 distinct hour_start, 1 distinct day_start; October has 1 distinct month_start
def test_spring_day_has_92_intervals(tmp_path):   # generate 2026-03-23..2026-04-05; local_date 2026-03-29: 92
def test_orders_never_overlap_on_a_line(con)      # self-join on line_id with overlapping [start, end): zero rows
def test_orders_on_quarter_hours(con)             # minute in (0, 15, 30, 45), seconds 0
def test_solar_nonnegative_battery_signed(con)    # min(solar) >= 0; battery has both signs
def test_band_matches_definition(con)             # sample rows: Sunday noon F3, Tuesday 10:00 F1, Saturday 10:00 F2
def test_every_meter_has_every_interval(con)      # count per meter equal, no NULL kwh
def test_no_orders_on_sunday(con)
def test_load_follows_production(con)             # for line_a1: mean kWh of intervals inside an order > 2 x mean kWh of intervals outside any order
def test_weekday_day_above_night_above_nothing(con) # site: mean kWh Tue 10:00-12:00 > mean kWh Tue 02:00-04:00 >= 41 * 0.5 * 0.9
def test_sunday_is_standby(con)                   # site: total on Sunday 2026-10-25 (per interval mean) < 0.6 x Tuesday 2026-10-27
def test_closure_has_no_orders(tmp_path)          # generate 2025-12-20..2026-01-10: no order overlaps 2025-12-24..2026-01-06
def test_solar_zero_at_night_and_seasonal(tmp_path) # zero between 23:00 and 04:00 local; a June day total > a December day total (two short generated spans)
def test_two_lines_peak_at_different_instants(con) # line_a1 and line_b1: argmax ts over the fixture differ
```

- [ ] **Step 2:** `uv run pytest tests/test_generator.py` fails on import.
- [ ] **Step 3:** Implement `generate` and the CLI.
- [ ] **Step 4:** Tests pass. `uv run python -m agentic_ems.generator` finishes in under two minutes; note the time and file size in the commit body.
- [ ] **Step 5:** Commit: "Add the seeded plant generator" (why: every later number must be reproducible from one seed).

### Task 2: Semantic layer declaration and request validation

**Files:** Create `src/agentic_ems/semantic.py`, `tests/test_semantic_validate.py`.

**Interfaces — Produces:**
```python
LEVELS = ("site", "department", "line", "machine")
GRAINS = ("15min", "hour", "day", "month", "total")
METRICS: dict[str, Metric]   # consumption_kwh, peak_kw, cost_eur, energy_per_unit; each with unit, description, allows_tariff_split
RULES: dict[str, str]        # R1 parent meters include their children; R2 a node total is its own meter, never a sum of lower levels;
                             # R3 peak is coincident and is interval kWh times 4; R4 cost uses the tariff of each interval;
                             # R5 energy per unit is a ratio of totals with order units spread over the order; R6 it is null without units;
                             # R7 the four metrics read consumption meters only; R8 timestamps are UTC interval starts;
                             # R9 days, months and bands use the local-time columns; R10 a period includes its start and excludes its end
class RequestError(ValueError)
class MetricRequest(pydantic.BaseModel):   # extra="forbid"
    metric: Literal[...]; node: str | None = None; breakdown: Literal[...] | None = None
    meters: list[str] | None = None; start: date; end: date
    grain: Literal[...] = "total"; by_tariff_band: bool = False
@dataclass
class Catalog: meters: dict[str, Meter]; first_date: date; end_date: date
def load_catalog(con) -> Catalog
def validate(request: MetricRequest, catalog: Catalog) -> None   # raises RequestError
def catalogue_text(catalog) -> str; def meter_tree_text(catalog) -> str; def tool_schema() -> dict
```
`node` and `meters` are mutually exclusive; with neither, the node is `site`. `tool_schema()` is `MetricRequest.model_json_schema()`; `catalogue_text` is rendered from `METRICS` and `RULES`.

- [ ] **Step 1:** Write tests, each expecting `RequestError` with the quoted fragment in the message:

```python
def test_list_with_ancestor_is_rejected       # meters=["dept_a","line_a2"] -> names both meters
def test_list_with_repeat_is_rejected         # meters=["line_a1","line_a1"]
def test_generation_meter_is_rejected         # node="solar" and meters=["site","solar"] -> "consumption meters only"
def test_breakdown_not_below_node             # node="line_a1", breakdown="department"
def test_unknown_meter                        # node="dept_z"
def test_node_and_meters_together
def test_period_outside_dataset               # partly or wholly outside; message: data from first_date up to but excluding end_date, both ISO
def test_start_not_before_end                 # start == end, and start > end
def test_tariff_split_only_consumption_and_cost
def test_energy_per_unit_scope                # ok: node line; node site or department with breakdown="line". Rejected: machine, list, no breakdown on site
def test_hostile_strings                      # node="site'; DROP TABLE meter;--" -> unknown meter
def test_valid_requests_pass                  # one per metric
def test_tool_schema_lists_closed_sets        # enums equal METRICS keys, LEVELS, GRAINS
```

- [ ] **Step 2:** Fails on import. **Step 3:** Implement. **Step 4:** Passes.
- [ ] **Step 5:** Commit: "Declare metrics and hierarchy rules in one file" (why: the rule that a meter is never added to its ancestor must be readable in one place).

### Task 3: Compiler

**Files:** Modify `src/agentic_ems/semantic.py`. Create `tests/test_semantic_compile.py`.

**Interfaces — Produces:**
```python
@dataclass
class Compiled: sql: str; params: list; columns: list[str]
def compile(request: MetricRequest, catalog: Catalog) -> Compiled   # pure; calls validate first
```
Column order: `meter_id` (only with a breakdown), `bucket_start` (every grain except `total`), `tariff_band` (only with the split), `value`. Rows ordered by `meter_id`, then the bucket's earliest `ts_utc` (not the string), then `tariff_band`. All values are bound parameters; identifiers come only from closed sets. Bucket columns: `local_start`, `hour_start`, `day_start`, `month_start`. Period filter on `local_date >= start AND local_date < end`.

**Approach where the tests do not decide:** `peak_kw` sums kWh per `ts_utc` across the request's meters, multiplies by 4, then takes the max per bucket. `energy_per_unit` LEFT JOINs each line reading to the order covering that interval on the same line, with `units / number of intervals in the order` as the interval's units; idle intervals keep their kWh and add zero units. Total kWh is divided by total units per cell; `NULLIF` makes zero units a NULL, so a cell without orders is a row with a NULL value.

- [ ] **Step 1:** Write tests that execute the compiled SQL on the fixture database and compare with a direct computation written in the test:

```python
def test_site_total_is_site_meter_only         # equals sum(kwh) where meter_id='site'; differs from site+departments by > 3%
def test_breakdown_has_one_row_per_descendant  # node=site, breakdown=line -> 10 rows, meter_ids are the 10 lines
def test_list_of_two_lines_is_their_sum
def test_hourly_on_autumn_day_has_25_buckets   # 2026-10-25, grain hour: 25 distinct bucket_start, two of them at local 02:00 with different offsets
def test_period_end_is_excluded
def test_peak_is_coincident                    # two lines: value <= sum of the two separate peaks, and equals max over ts of (a+b)*4
def test_cost_uses_interval_tariff             # equals sum(kwh * price) joined on tariff_band
def test_tariff_split_rows                     # three rows F1,F2,F3 whose sum equals the unsplit value
def test_energy_per_unit_is_ratio_of_totals    # equals total kWh / total prorated units, not avg of daily ratios
def test_energy_per_unit_null_without_units    # grain day over a week containing Sunday 2026-10-25 -> that day's row exists and its value is None
def test_compile_is_pure                       # same request twice -> identical sql and params; no connection used
def test_no_literal_values_in_sql              # meter ids and dates appear only in params
```

- [ ] **Step 2:** Fails. **Step 3:** Implement. **Step 4:** Passes.
- [ ] **Step 5:** Commit: "Compile structured requests to SQL" (why: one deterministic function produces every query the agent, MCP server and demo run).

### Task 4: `query`, the read-only connection and the limits

**Files:** Modify `src/agentic_ems/semantic.py`, `tests/conftest.py` (`con` becomes `open_db(db_path)`). Create `tests/test_semantic_query.py`.

**Interfaces — Produces:**
```python
DEFAULT_DB = Path(__file__).resolve().parents[2] / "data" / "plant.duckdb"; ROW_LIMIT = 5000; TIME_LIMIT_MS = 10000
@dataclass
class QueryResult: columns: list[str]; rows: list[tuple]; sql: str; params: list
class LimitError(RuntimeError)
def open_db(path: Path = DEFAULT_DB) -> duckdb.DuckDBPyConnection
def execute_limited(con, sql: str, params: list | None = None) -> tuple[list[str], list[tuple]]
def query(request: MetricRequest | dict, con, catalog: Catalog) -> QueryResult
```
`open_db` opens read-only, then sets `enable_external_access = false`, `autoinstall_known_extensions = false`, `autoload_known_extensions = false`, `max_execution_time = 10000`, then `lock_configuration = true`. Confirm each setting name in the DuckDB 2.0 docs first. The settings lock the whole database instance, so `open_db` is wrapped in `functools.cache`: one open and one lock per path per process, and every caller (agent, MCP server, demo, tests) reuses that connection. Check in the docs how a second connection to the same file in one process behaves before relying on anything else. The engine's timeout error is re-raised as `LimitError` naming 10 seconds; if the docs do not confirm `max_execution_time`, use a timer that calls the connection's documented interrupt method. A missing file raises `FileNotFoundError("data/plant.duckdb not found. Run: uv run python -m agentic_ems.generator")`. `execute_limited` fetches 5,001 rows and raises `LimitError` naming the limit on the 5,001st. A dict that fails pydantic validation becomes a `RequestError` with a short message.

- [ ] **Step 1:** Tests: `test_query_returns_rows_and_sql`; `test_dict_request_is_validated` (unknown field, wrong type); `test_missing_database_names_the_command`; `test_row_limit` (site breakdown by machine at `15min` over 13 days -> `LimitError` mentioning 5,000; `execute_limited` on `SELECT * FROM range(5000)` passes and on `range(5001)` raises); `test_time_limit_is_set` (`SELECT current_setting('max_execution_time')`); `test_timeout_is_a_limit_error` (a cross join that cannot finish, with the limit patched to 200 ms -> `LimitError` mentioning seconds); `test_open_db_is_cached` (two calls return the same object); `test_database_is_read_only` (`CREATE TABLE` fails); `test_database_readable_after_lockdown`.
- [ ] **Step 2:** Fails. **Step 3:** Implement. **Step 4:** Passes.
- [ ] **Step 5:** Commit: "Add query as the single entry point" (why: connection, row limit and time limit live in one place every caller shares).

### Task 5: `run_sql`, the text-to-SQL arm's documentation

**Files:** Create `src/agentic_ems/benchmark/__init__.py`, `src/agentic_ems/benchmark/sql_arm.py`, `docs/schema.md`, `tests/test_sql_arm.py`, `tests/test_schema_doc.py`.

**Interfaces — Consumes:** `open_db`, `execute_limited`, `RULES`. **Produces:** `run_sql(con, sql: str) -> QueryResult` (raises `RequestError` or `LimitError`; database errors are re-raised as `RequestError` carrying the engine's message).

`docs/schema.md` is written by hand: every table and column of Task 1 with type and meaning, the band definitions, and one paragraph per rule in `RULES`, each starting with its id in the form `**R3.**`. It must give a careful SQL writer everything needed to answer correctly. It never calls `run_sql` sandboxed.

- [ ] **Step 1:** Tests:

```python
def test_select_works
@pytest.mark.parametrize("sql", [
  "INSERT INTO meter VALUES ('x','x','site',NULL,'consumption')",
  "SET lock_configuration = false", "SET enable_external_access = true",
  "ATTACH '{other_db}' AS o", "COPY meter TO '{out_csv}'",          # both paths under tmp_path; other_db and a small CSV exist
  "SELECT * FROM read_csv('{some_csv}')", "SELECT 1; SELECT 2",
  "INSTALL httpfs", "CREATE TABLE t AS SELECT 1"])
def test_statement_is_refused(con, sql)        # RequestError; then SELECT count(*) FROM meter still works; out_csv was not created
def test_database_barrier_without_the_parser    # the ATTACH, COPY, read_csv, SET lock_configuration and INSTALL cases through execute_limited directly: each raises a DuckDB error
def test_row_limit_applies
def test_every_rule_id_is_in_schema_doc        # for id in RULES: f"**{id}.**" in docs/schema.md
def test_every_column_is_in_schema_doc         # every table.column from information_schema appears in docs/schema.md
```

- [ ] **Step 2:** Fails. **Step 3:** Implement `run_sql` and write `docs/schema.md`. `run_sql` first parses with `sqlglot.parse(sql, read="duckdb")`, drops `None` entries, and refuses anything that is not exactly one read statement (a `SELECT`, a set operation of selects, or a `WITH` whose body is one and whose CTEs modify nothing); a `ParseError` is a refusal with its message. The database settings stay as the second barrier. `run_sql` executes the original text, never sqlglot's re-rendering. Add tests that a top-level `WITH ... SELECT` and a `UNION ALL` are accepted. **Step 4:** Passes.
- [ ] **Step 5:** Commit: "Add the locked-down SQL tool and the schema documentation" (why: the baseline arm must be strong and must not be able to touch the host).

### Task 6: The agent loop and the two arms

**Files:** Create `src/agentic_ems/agent.py`, `tests/test_agent.py`, `tests/fakes.py`. Modify `src/agentic_ems/benchmark/sql_arm.py`, `tests/conftest.py`.

Load the `claude-api` skill for the model id, effort values and caching rules. Read the LangGraph quickstart and streaming pages and the `ChatAnthropic` integration page before writing.

**Interfaces — Produces:**
```python
MODEL = "claude-opus-5-5"      # confirm against the claude-api skill on the day
@dataclass
class ToolResult: ok: bool; content: str; columns: list[str] | None = None; rows: list[tuple] | None = None; sql: str | None = None
@dataclass
class Arm: name: str; system: list[dict]; tools: list[dict]; exec_tool: str; execute: Callable[[dict], ToolResult]
@dataclass
class RunRecord:
    arm: str; question: str; messages: list[dict]; calls: list[dict]
    final: Literal["rows", "refusal", "none"]; columns: list[str] | None; rows: list[tuple] | None
    successful_results: list[tuple[list[str], list[tuple]]]; refusal_reason: str | None
    cause: str | None            # set only when final == "none": "max_tool_calls", "max_tokens", "provider_refusal", "no_tool_call"
    usage: dict[str, int]        # input_tokens, cache_creation_input_tokens, cache_read_input_tokens, output_tokens, summed
    served_model: str
class ModelMismatch(RuntimeError)
def make_model(*, model: str = MODEL, effort: str, max_tokens: int) -> ChatAnthropic
def semantic_arm(con, catalog, say_today: bool = False) -> Arm          # tools query_metric, refuse; say_today adds catalog.end_date as "today", demo only
def render_rows(columns: list[str], rows: list[tuple], max_rows: int = 50) -> str   # CSV of the first max_rows plus a line with the total row count; both arms use it for tool results
def sql_arm(con, catalog, schema_md: str) -> Arm                        # in sql_arm.py; tools run_sql, refuse
def build_graph(model: BaseChatModel, arm: Arm, max_tool_calls: int = 4)   # compiled StateGraph, nodes "llm_call" and "tool_node", no checkpointer
def run_agent(model: BaseChatModel, arm: Arm, question: str, *, requested_model: str = MODEL, max_tool_calls: int = 4, callbacks: list | None = None, on_text: Callable[[str], None] | None = None) -> RunRecord
def tracing_callbacks() -> list   # [langfuse.langchain.CallbackHandler()] when both Langfuse keys are set, else []
```
- `make_model` sets `max_retries=0`, `max_tokens`, `effort` and `thinking={"type": "adaptive"}` explicitly and leaves `temperature` unset, so both arms send the same request and no retry is hidden.
- Both arms share one instruction text except for the name of the execution tool: 4 tool calls in total, the last successful execution call is the answer, call `refuse` with a reason when the question cannot be answered. `Arm.system` is a list of text blocks whose last block carries the one `cache_control` breakpoint. Tools are bound with `bind_tools` and no `tool_choice`.
- How a run ends, in this order. Tool calls inside one message run in order and each counts; calls past the 4th are not run. A `refuse` call ends the run after the calls before it, with `final="refusal"`. A response with `stop_reason` `refusal` or `max_tokens` gives `final="none"` whatever came before. Otherwise, when the model stops calling tools or the cap is reached: `final="rows"` with the last successful execution if there was one, else `final="none"` with cause `max_tool_calls` or `no_tool_call`. The conditional edge checks `stop_reason` before `tool_calls`. Tool errors (`RequestError`, `LimitError`, schema failures) go back as an error tool message and count as a call.
- Without `on_text`, `run_agent` uses `graph.invoke` and reads the four counts from each response's `response_metadata["usage"]`, the provider's own dict. With `on_text` (the demo) it uses `graph.stream(..., stream_mode="messages")`; usage and the model check are not needed there. In the benchmark path the served model is `response_metadata["model"]`; compare it with `requested_model` the way the `claude-api` skill says the API echoes model ids (exact id or dated form of the alias), and raise `ModelMismatch` otherwise.
- `RunRecord.messages` is plain JSON-serialisable dicts, converted from the LangChain messages, so transcripts do not depend on the library's classes.

`tests/fakes.py`: `ScriptedChatModel(BaseChatModel)` returns scripted `AIMessage`s with `tool_calls` and `response_metadata` (`usage`, `model`, `stop_reason`) and records the messages and tools it was given. Follow the `langchain-core` docs for the minimum a chat model subclass must implement.

- [ ] **Step 1:** Tests with `ScriptedChatModel`:

```python
def test_one_query_then_text_ends_with_rows
def test_compile_error_is_returned_and_model_can_retry     # ancestor list -> error message -> valid request -> final rows
def test_malformed_tool_input_is_an_error_result           # missing 'metric'; loop continues
def test_refuse_ends_the_run
def test_fifth_tool_call_is_not_executed                   # no earlier success -> final "none", cause "max_tool_calls"; with an earlier success -> final "rows"
def test_calls_in_one_message_run_in_order_and_count       # 3 calls then 2 in the next message -> only the first of the 2 runs
def test_refuse_after_a_query_in_the_same_message          # final "refusal"
def test_max_tokens_after_rows_is_still_no_answer
def test_last_successful_call_is_the_answer                # two successes -> second; both in successful_results
def test_provider_refusal_is_no_answer                     # stop_reason "refusal", even with tool_calls present -> cause "provider_refusal"
def test_max_tokens_is_no_answer
def test_text_without_tool_call_is_no_answer               # cause "no_tool_call"
def test_wrong_served_model_raises
def test_usage_is_summed_from_raw_usage_dict               # all four counts
def test_system_has_one_cache_breakpoint_and_no_tool_choice
def test_arms_differ_only_in_context_and_exec_tool         # with say_today False
def test_both_arms_render_rows_identically                 # same rows -> same tool message text; 51 rows -> 50 lines plus the count line
def test_semantic_arm_never_exposes_sql_tool
def test_make_model_settings                               # dummy key in env: max_retries == 0, temperature is None, thinking and max_tokens set
def test_no_tracing_without_keys                           # tracing_callbacks() == [] with keys unset
def test_messages_are_json_serialisable
```

- [ ] **Step 2:** Fails. **Step 3:** Implement. **Step 4:** Passes with no API key and with `LANGFUSE_TRACING_ENABLED=false` set in `tests/conftest.py`.
- [ ] **Step 5:** Commit: "Add one tool loop shared by both arms" (why: a comparison is only fair if the loop, limits and instructions are identical).

### Task 7: Question set (owner gate)

**Input:** `docs/golden-questions-draft.md` after the owner's review, and `docs/problem-classes.md` if it exists. Do not start before the owner says the review is done.

**Files:** Create `src/agentic_ems/benchmark/questions.toml`, `docs/review-log.md`, `tests/test_questions.py`. Delete `docs/golden-questions-draft.md` in the same commit. The draft's table "Error classes and what version 1 does with each" moves into `docs/golden.md` in Task 9. `docs/problem-classes.md` is ignored by git and is never committed or quoted.

**Interfaces — Produces:** one table per question: `id` (H1.., C1.., M1.., O1.., U1..), `group` (`hierarchy`, `control`, `metric_definition`, `out_of_scope`, `unanswerable`), `ask` (the question in the words a person at the plant would type), `keys` (subset of `meter_id`, `bucket_start`, `tariff_band`), `source` (`public standard`, `author, from practice`, or `error class Pn`), `mistake`. A `[[rejected]]` list holds dropped questions with their reason. `load_questions() -> list[Question]` lives in `scorer.py`'s neighbour `verify.py` (Task 9); this task only writes the file and its test.

**How `ask` is written.** As a person at the plant would type it, not as a specification: meter names as people say them (`the paint shop`, `line 2 in welding`, `Press 3`), never meter ids; dates the way people write them but absolute (`March 2026`, `the week of 12 October 2026`, `on 25/10/2026`); mixed registers across the set (a terse note, a full sentence, a question from a manager about cost, one from maintenance about a machine); the trap questions say the trap the way people do (`everything in welding added up`). No two questions share a template. The mix follows how often each type is asked, from the frequency column of the reviewed error-class list: department and plant totals, daily and monthly trends, band splits and department comparisons get the most questions. Read `docs/problem-classes.md` for that column only; do not copy any sentence from it.

**What the model receives.** `text = ask + "\n\n" + shape_line(keys)`, where `shape_line` is one function in `verify.py` (Task 9; until then the test builds it the same way) that states the answer columns in a fixed sentence, for example `Answer with columns: meter_id, bucket_start, value.` The user's words are never edited to carry the format, and both arms get the same `text` byte for byte.

**What may be claimed.** The questions are written to read like the ones plant staff ask and their types and frequencies come from an owner-reviewed list. They are not logs of real users, and `docs/golden.md` says so in those words.

Every period lies inside 2025-11-01 to 2026-11-01. H19 and H20 have a defined correct answer decided by the owner. Any question whose correct outcome is a refusal (the draft's H21 and U-rows) is in the `unanswerable` group; a question that stays in `hierarchy` has rows as its answer. No question asks for an order. If `/eval-plan` later cuts a group, the cut questions move to `rejected` with that reason. `docs/review-log.md` records the date and which questions the owner read, changed and rejected.

- [ ] **Step 1:** `tests/test_questions.py`: ids unique; every group present; every `keys` value in the closed set; no `ask` contains a meter id, the word `value` or a column name; no two `ask` strings share their first four words; every date in a text is inside the dataset, except in the `unanswerable` group; unanswerable has 3 to 8 questions.
- [ ] **Step 2:** Write the files. **Step 3:** Test passes.
- [ ] **Step 4:** Commit: "Add the reviewed question set" (why: the questions and their provenance are fixed before any model is run on them).

### Task 8: Reference queries and known wrong answers

**Files:** Create `src/agentic_ems/benchmark/reference_sql.toml`. Do not open `reference_py.py`. Do not run or copy the output of `compile()`: write each query from the spec and `docs/schema.md`.

**Produces:** per answerable question id: `sql` (hand-written against the raw tables, returning the canonical columns of Task 3) and, for hierarchy questions, `[[wrong]]` entries with `category` (`double_count`, `children_sum`, `separate_peaks`, `extra_rows`) and `sql` for the complete wrong row set. Out-of-scope questions have `sql` and no `wrong`. Unanswerable questions have no entry. Every hierarchy question has at least one `wrong`. Every hierarchy, control and metric-definition question also has a hand-written `request` table (the `MetricRequest` a correct agent would send), used by Task 10 to check that the compiler agrees with the reference.

- [ ] **Step 1:** Write the file. **Step 2:** Every query runs on the full database through `execute_limited` without error (a throwaway loop in the shell; the permanent test is in Task 9).
- [ ] **Step 3:** Commit: "Add hand-written reference queries and known wrong answers" (why: ground truth is written by hand, never produced by an arm).

### Task 9: Second computation and the one-command verification

**Dispatch to an implementer who has not seen Task 8's file and is told not to open `reference_sql.toml`.** It works from `questions.toml`, `docs/schema.md` and the spec.

**Files:** Create `src/agentic_ems/benchmark/reference_py.py`, `src/agentic_ems/benchmark/verify.py`, `docs/golden.md`, `tests/test_verify.py`.

**Interfaces — Produces:**
```python
# reference_py.py: plain Python over raw rows fetched with a plain SELECT filtered by meter and period only
# (no aggregation, no join in SQL), through its own read-only connection and fetchall, not execute_limited
ANSWERS: dict[str, Callable[[Data], tuple[list[str], list[tuple]]]]     # one per answerable question id
# verify.py
@dataclass
class Question: id: str; group: str; text: str; keys: list[str]; source: str; mistake: str
@dataclass
class Truth: reference: tuple[list[str], list[tuple]] | None; wrong: list[tuple[str, list[str], list[tuple]]]
def load_questions() -> list[Question]
def load_truth(con) -> dict[str, Truth]
def main() -> int     # uv run python -m agentic_ems.benchmark.verify
```
`main` regenerates the database from the seed, computes every answer both ways, compares them with the scorer's tolerance (Task 10's `rows_match`; until Task 10 lands, a local comparison with the same numbers: relative 0.1%, absolute 0.001 at zero), checks every wrong answer differs from its reference by more than the tolerance, prints one line per question and exits non-zero on any disagreement.

`docs/golden.md`, one page: how the set was built, what was verified and how (two computations from one specification, generator invariants, owner hand-check with its date), what was not, the limits (one synthetic plant, one model, one run date). It does not say "independently verified".

- [ ] **Step 1:** `tests/test_verify.py` on `full_db_path`: both computations agree for every answerable question; every answerable id has both a query and a Python answer; no unanswerable id has either; each `wrong` category is in the closed set and every hierarchy question has at least one; every wrong row set differs from its reference by more than the tolerance; totals known by construction hold (for one day, site kWh equals the sum of machine kWh times the product of the margins on each path, within rounding).
- [ ] **Step 2:** Fails. **Step 3:** Implement. **Step 4:** Tests pass and `uv run python -m agentic_ems.benchmark.verify` exits 0 on the full database. The implementer does not open `reference_sql.toml` to resolve a disagreement: it reports the question id and both row sets, and the controller decides which side is wrong, has that side fixed, and records it in `docs/review-log.md`. If a `separate_peaks` wrong value equals its reference, the question's period or meters change, not the tolerance.
- [ ] **Step 5:** Commit: "Verify every reference answer twice with one command" (why: a reader can rebuild and re-check the ground truth offline).

### Task 10: Scorer

**Files:** Create `src/agentic_ems/benchmark/scorer.py`, `tests/test_scorer.py`. Modify `verify.py` to use `rows_match`.

**Interfaces — Produces:**
```python
REL_TOL = 0.001; ABS_TOL_AT_ZERO = 0.001
@dataclass
class Score: outcome: Literal["correct", "known_wrong", "other_wrong", "refusal", "no_answer"]; category: str | None; correct: bool; earlier_correct: bool
def rows_match(columns, rows, ref_columns, ref_rows, keys: list[str]) -> bool
def score(question: Question, truth: Truth, record: RunRecord) -> Score
```
Rules are the spec's "What is scored" and "Outcomes" paragraphs, exactly. `bucket_start` is compared as a parsed instant; a value without a UTC offset is `other_wrong`. `int`, `float` and `Decimal` values are compared numerically. `correct` is true for `correct`, and for `refusal` only in the unanswerable group; rows in the unanswerable group are `other_wrong`.

- [ ] **Step 1:** Tests:

```python
def test_reference_scores_correct_for_every_question        # self-test on full_db_path: 100%
def test_compiler_agrees_with_every_reference               # each question's hand-written request through query() scores correct
def test_empty_and_constant_answers_score_zero
def test_each_known_wrong_lands_in_its_category
def test_tolerance_edges                                    # 0.09% off correct, 0.11% off not; reference 0 with 0.0005 correct; reference 0.5 with 0.5008 not correct
def test_null_matches_only_null
def test_extra_columns_ignored_missing_column_other_wrong
def test_row_order_ignored
def test_offset_forms_of_same_instant_match                 # "2026-10-25T02:00:00+02:00" vs "2026-10-25T00:00:00+00:00"
def test_extra_or_missing_rows_never_correct
def test_unexpected_shapes_do_not_raise                     # duplicate keys, text value, NaN, zero rows, bucket_start without offset -> other_wrong
def test_refusal_correct_only_when_unanswerable
def test_no_answer_causes                                   # final "none" with each cause -> no_answer
def test_earlier_correct_flag                               # first success correct, last not
```

- [ ] **Step 2:** Fails. **Step 3:** Implement. **Step 4:** Passes.
- [ ] **Step 5:** Commit: "Score runs on returned rows only" (why: the model's prose is never trusted, and the scorer proves itself before any paid run).

### Task 11: Benchmark harness and report

**Files:** Create `src/agentic_ems/benchmark/run.py`, `src/agentic_ems/benchmark/report.py`, `tests/test_run.py`, `tests/test_report.py`, `tests/fixtures/results_small.json`.

**Interfaces — Produces:**
- `uv run python -m agentic_ems.benchmark.run --repeats N --min-repeats M --budget-eur X --first-repeat-eur F --eur-per-usd R --effort E --max-tokens T [--arm NAME] [--only ID ...]`. Writes `results/results.json`, `results/errors.jsonl` and `results/runs/<run_id>/`: one `<question>-<arm>-<repeat>.json` transcript per run, plus each arm's fixed prompt once. All of it is committed, so every number traces to a transcript.
- `run_benchmark(model, questions, truths, arms, *, repeats, budget_eur, eur_per_usd, ...) -> dict` so tests can pass `ScriptedChatModel`. Paid runs pass `tracing_callbacks()`.
- `results.json`: `versions` (seed, sha256 of `questions.toml`, `scorer.py`, both system prompts, model id, effort), `eur_per_usd`, `prices`, `min_repeats`, `first_repeat_eur`, and one entry per run: question, arm, repeat, outcome, category, earlier_correct, the four token counts, cost_eur, retries.
- `report.py`: `build_block(results: dict) -> str` and `uv run python -m agentic_ems.benchmark.report --write-readme`, which replaces the text between `<!-- results:start -->` and `<!-- results:end -->` in `README.md`.

Order of runs: repeat 1 of every question in both arms, then repeat 2. Before each repeat, the estimated cost of one more repeat (mean cost so far, or `--first-repeat-eur` for the first) must fit the remaining budget, else stop. The model has `max_retries=0`, so the harness does all retrying: API errors, rate limits and timeouts are retried with backoff and jitter, logged to `errors.jsonl`, and never scored. Each run has a wall-clock limit (set with the token cap in `/eval-plan`); exceeding it is a harness failure. A `ModelMismatch` fails the attempt the same way, and three in a row abort the run. Fallback to another model is never enabled. The run fails if neither arm ever reads from cache. Prices come from the `claude-api` skill.

Report block: first, per arm, runs that delivered a wrong value without warning (`known_wrong` plus `other_wrong`) over runs scored. Then per arm and group: accuracy with its 95% interval, refusals, known wrong by category, no-answer, cost, cost per correct answer, and runs with `earlier_correct`. Then, per group, the paired difference between arms with a 95% interval from a bootstrap over questions (10,000 resamples, fixed seed, each question's accuracy averaged over its repeats). Unanswerable is shown as counts. The block also renders the example (question, the SQL that ran, the wrong value, the reference value, link to the transcript) from the first `double_count` run in the results. Below `min_repeats`, the block shows counts and says no comparison is claimed. If no run has `double_count`, the block says so in the spec's words.

- [ ] **Step 1:** Tests with `ScriptedChatModel`: run order by repeat; stop when the next repeat would exceed the budget, with equal run counts per arm; an injected API error is retried, logged and not scored; a run over the wall-clock limit is a logged failure; one `ModelMismatch` fails the attempt and three abort; cost uses all four token counts; versions are recorded; `build_block(fixture)` equals a stored expected string; bootstrap interval is deterministic; the below-minimum and no-double-count wordings appear when they should.
- [ ] **Step 2:** Fails. **Step 3:** Implement. **Step 4:** Passes.
- [ ] **Step 5:** Commit: "Add the benchmark harness and the generated report" (why: every published number must trace to a results file and a transcript).

### Task 12: Pilot of the baseline arm (owner gate, paid)

Not a code task. Before it, the owner reads `docs/schema.md` and hand-checks a sample of reference answers; both are logged with their date in `docs/review-log.md`. Then run the `/eval-plan` skill for a pilot of the text-to-SQL arm on about 8 hierarchy questions, 1 repeat, with `run --arm sql --only <ids> --repeats 1`; it is paid from the 20 EUR. Also count SQL that DuckDB would accept but the `sqlglot` check refused, so the baseline is not penalised by the guard. It fixes the effort level, the token cap per question, the EUR conversion rate and the stop rules, dry-runs at zero cost, and shows a spend table. Stop for the owner's approval before the first paid call. The first paid response also settles one open fact: `response_metadata["usage"]["cache_creation_input_tokens"]` must be non-zero on the first call of an arm whose fixed part is above the minimum cacheable length the `claude-api` skill gives for the model; if it is zero, stop and investigate before spending more. Record the outcome counts in `docs/review-log.md` and commit them: "Record the baseline pilot" (why: whether the baseline double counts at all decides what the README can claim). If no run double counts, tell the owner before continuing; the spec says the result is published whatever it shows.

### Task 13: Full benchmark run (owner gate, paid)

Run `/eval-plan` for the full run: it fixes questions per group, repeats, the minimum repeats, and the smallest difference worth reporting, then dry-runs and prices a three-item pilot. Stop for the owner's approval. After the run, commit `results/results.json` and `results/example/`: "Record the benchmark results" (why: the result is published whatever it shows).

### Task 14: MCP server

Read the `mcp` 2.x pages listed in `docs/stack.md` first. The server class is `MCPServer`; nothing is printed to stdout.

**Files:** Create `src/agentic_ems/mcp_server.py`, `tests/test_mcp_server.py`. Add a `[project.scripts]` entry `agentic-ems-mcp`.

**Produces:** tools `list_metrics() -> str` (`catalogue_text`), `list_meters() -> str` (`meter_tree_text`), `query_metric(request: MetricRequest) -> dict` (columns, rows, sql; a `RequestError` or `LimitError` is returned as an error result with its message).

- [ ] **Step 1:** Tests call the three tool functions directly on the fixture database, including one ancestor-list request that returns the error, and one in-process client round trip using the SDK's documented test client if the docs give one.
- [ ] **Step 2:** Fails. **Step 3:** Implement. **Step 4:** Passes.
- [ ] **Step 5:** Commit: "Expose the semantic layer over MCP" (why: any MCP client gets the same guarantees as the agent, through the same function).

### Task 15: Local demo server and saved answers

**Files:** Create `src/agentic_ems/demo.py`, `tests/test_demo.py`, `frontend/public/samples/` (four JSON files, written by the recorder).

**Interfaces — Produces:**
- `uv run python -m agentic_ems.demo serve` binds `127.0.0.1:8000` only. Routes: `GET /api/health` -> `{"ok": true, "has_key": bool}`; `POST /api/ask` with `{"question": str}` -> server-sent events `text` (delta), `request` (structured request and compiled SQL), `rows` (columns, rows), `refusal` (reason), `error` (one line), `done`; everything else serves `frontend/dist` when that folder exists (tests do not need it).
- `uv run python -m agentic_ems.demo record` (paid, development cap, owner approval): runs the semantic arm on the four sample questions and writes `frontend/public/samples/<n>.json` with `question`, `text`, `request`, `sql`, `columns`, `rows`, and `index.json` listing them.
- `MAX_QUESTION_CHARS = 500`. Empty or longer questions get an `error` event and no API call. No key in the environment: `error` event naming `ANTHROPIC_API_KEY`. The demo builds its arm with `say_today=True`. Questions are not logged.

- [ ] **Step 1:** Tests with Starlette's test client and `ScriptedChatModel` injected as the model: event order for an answered question; refusal; empty and 501-character questions make no model call; missing key message; server never exposes `run_sql`; every file in `frontend/public/samples/` has a `request` that `query` answers with exactly the saved rows.
- [ ] **Step 2:** Fails. **Step 3:** Implement. Record the samples only after the owner approves the spend. **Step 4:** Passes.
- [ ] **Step 5:** Commit: "Add the local demo server and saved sample answers" (why: free-text runs on the reader's own key, and the samples cost nothing to replay).

### Task 16: Frontend page

Read the shadcn/ui Vite install page and chart component page first. Load the `frontend-design:frontend-design` skill only for spacing and type; no theming, no design system, no extra panels.

**Files:** Create `frontend/` (Vite, React, TypeScript, Tailwind, shadcn `button`, `input`, `card`, `chart`), `frontend/src/App.tsx`, `frontend/src/stream.ts`, `frontend/src/stream.test.ts`.

**Behaviour:** One page with four elements: question input with the four sample buttons, streamed answer, chart, structured request with the compiled SQL inside the same panel. The headline number is rendered from the rows, formatted in code; the model's text is shown under it. Chart: line for rows with `bucket_start`, bars for rows with `meter_id` or `tariff_band` and no `bucket_start`, none for a single value, a refusal or an error. On load the page calls `GET /api/health`; if it fails, free text is disabled and one line reads "Recorded answers. For your own questions, clone the repository and run `uv run python -m agentic_ems.demo serve`." Vite `base` is `./` and samples are fetched with relative paths, so the same build works on localhost and under a GitHub Pages sub-path. A sample click replays its JSON as a stream with a timer; in local mode a typed question goes to `POST /api/ask`. States: streaming, out of scope, no API key, error.

`stream.ts` exports `replay(sample, onEvent, delayMs)`, `ask(question, onEvent)` and `chartKind(columns, rows)`, both emitting the event names of Task 15, so `App.tsx` has one code path.

- [ ] **Step 1:** `stream.test.ts` (Vitest): `replay` emits `text` deltas that concatenate to the saved text, then `request`, `rows`, `done`, in that order; `chartKind(columns, rows)` returns `line`, `bar` or `none` for the three shapes.
- [ ] **Step 2:** Fails. **Step 3:** Implement. **Step 4:** `npm test` and `npm run build` pass; with `demo serve` running, open the page with the gstack `/browse` skill and check a sample click and the disabled-input line on the static build.
- [ ] **Step 5:** Commit: "Add the one-page demo" (why: the reader's first click must work without a key or a server).

### Task 17: README, video script and workflows

**Files:** Create `README.md`, `docs/video-script.md`, `.github/workflows/test.yml`, `.github/workflows/pages.yml`, `tests/test_readme.py`.

- README, in this order: the opening sentence fixed by the spec's Constraints, word for word; the generated results block between its markers; (the block already contains the double-counting example or the spec's "no run produced it" statement); how to run (generate, test, verify, local demo, MCP); "Why this exists" with the spec's fixed text, word for word; "How this was built" with facts from the design document and `git log` only; limits; links to `docs/`. The repository is never called a port, extract or public version of anything. Every number outside the results block is absent or is a fact about the code.
- `test.yml`: on push, `uv sync`, `ruff check`, `mypy src`, generate, `uv run pytest`, `verify`. `pages.yml`: `workflow_dispatch` only, builds `frontend` and deploys `dist` to GitHub Pages. Neither is pushed in this task.
- `docs/video-script.md`: reads aloud in two minutes (at most 300 words).
- `tests/test_readme.py`: the block in `README.md` equals `build_block(results/results.json)`; the two fixed sentences are present verbatim; the phrases "a port of", "an extract of" and "public version" do not appear, in any letter case.

- [ ] **Step 1:** Write the test. **Step 2:** Fails. **Step 3:** Write the files and run `report --write-readme`. **Step 4:** `uv run pytest` passes in full.
- [ ] **Step 5:** Commit: "Add the README, video script and workflows" (why: the first screen carries the result and the proof that it regenerates).

---

## After the last task

In the order the brief sets: `/code-review high`, then `/cso` (or `/security-review` over the whole repository if `/cso` is unavailable), then `/document-release`. Then stop and ask the owner before publishing anything. Before that question, the owner runs the final name check of the clean-room plan from their side.
