import { afterEach, describe, expect, it, vi } from "vitest"
import { ask, chartKind, formatValue, headline, replay, unitOf, type StreamEvent } from "./stream"
import { breakdownSample, refusalSample, seriesSample, singleSample } from "./test-fixtures"

async function collect(run: (onEvent: (e: StreamEvent) => void) => Promise<void>): Promise<StreamEvent[]> {
  const events: StreamEvent[] = []
  await run((e) => events.push(e))
  return events
}

function texts(events: StreamEvent[]): string {
  return events.map((e) => (e.name === "text" ? e.data.delta : "")).join("")
}

describe("replay", () => {
  it("streams an answer as text deltas, then request, rows and done", async () => {
    const events = await collect((on) => replay(seriesSample, on, 0))
    const names = events.map((e) => e.name)
    const firstOther = names.findIndex((n) => n !== "text")
    expect(names.slice(0, firstOther).length).toBeGreaterThan(1)
    expect(names.slice(firstOther)).toEqual(["request", "rows", "done"])
    expect(texts(events)).toBe(seriesSample.text)
    expect(events[firstOther]).toEqual({
      name: "request",
      data: { request: seriesSample.request, sql: seriesSample.sql },
    })
    expect(events[firstOther + 1]).toEqual({
      name: "rows",
      data: { columns: seriesSample.columns, rows: seriesSample.rows },
    })
  })

  it("streams a refusal as text deltas, then refusal and done", async () => {
    const events = await collect((on) => replay(refusalSample, on, 0))
    const names = events.filter((e) => e.name !== "text").map((e) => e.name)
    expect(names).toEqual(["refusal", "done"])
    expect(texts(events)).toBe(refusalSample.text)
    expect(events.at(-2)).toEqual({ name: "refusal", data: { reason: refusalSample.refusal } })
  })
})

describe("chartKind", () => {
  it("draws a line for a time series, bars for a breakdown, nothing for a single value", () => {
    expect(chartKind(seriesSample.columns!, seriesSample.rows!)).toBe("line")
    expect(chartKind(breakdownSample.columns!, breakdownSample.rows!)).toBe("bar")
    expect(chartKind(["tariff_band", "value"], [["test_band", 1]])).toBe("bar")
    expect(chartKind(["meter_id", "bucket_start", "value"], [["m", "2001-01-01", 1]])).toBe("line")
    expect(chartKind(singleSample.columns!, singleSample.rows!)).toBe("none")
  })
})

describe("units and numbers", () => {
  it("maps each metric to its unit, and an unknown metric to none", () => {
    expect(unitOf("consumption_kwh")).toBe("kWh")
    expect(unitOf("peak_kw")).toBe("kW")
    expect(unitOf("cost_eur")).toBe("EUR")
    expect(unitOf("energy_per_unit")).toBe("kWh per unit")
    expect(unitOf("not_a_metric")).toBe("")
    expect(unitOf(undefined)).toBe("")
  })

  it("formats a value with its unit in code", () => {
    expect(formatValue(99999.9, "EUR")).toBe("100,000 EUR")
    expect(formatValue(12.345, "kWh")).toBe("12.35 kWh")
    expect(formatValue(0.12345, "kWh per unit")).toBe("0.123 kWh per unit")
    expect(formatValue(1234.5678, "")).toBe("1,235")
    expect(formatValue(null, "kWh")).toBe("no value")
  })

  it("takes the headline from the rows: the value, or the highest value and its label", () => {
    expect(headline(singleSample.columns!, singleSample.rows!)).toEqual({ value: 1234.5678, label: null })
    expect(headline(breakdownSample.columns!, breakdownSample.rows!)).toEqual({ value: 99999.9, label: "test_meter_b" })
    expect(headline(["meter_id", "bucket_start", "value"], [["m1", "d1", 1], ["m2", "d2", 5]])).toEqual({
      value: 5,
      label: "m2, d2",
    })
    expect(headline(["meter_id", "value"], [["m1", null]])).toEqual({ value: null, label: "m1" })
    expect(headline(["value"], [])).toBeNull()
  })
})

function sseResponse(chunks: string[], init: ResponseInit = {}): Response {
  const bytes = new TextEncoder()
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const chunk of chunks) controller.enqueue(bytes.encode(chunk))
      controller.close()
    },
  })
  return new Response(body, { status: 200, headers: { "Content-Type": "text/event-stream" }, ...init })
}

describe("ask", () => {
  afterEach(() => vi.unstubAllGlobals())

  it("posts JSON and parses events split across chunks in the middle", async () => {
    const wire =
      'event: text\ndata: {"delta": "Test é "}\n\n' +
      'event: text\ndata: {"delta": "fixture"}\n\n' +
      'event: request\ndata: {"request": {"metric": "peak_kw", "start": "2001-01-01", "end": "2001-01-02"}, "sql": "SELECT 1"}\n\n' +
      'event: rows\ndata: {"columns": ["value"], "rows": [[1.5]]}\n\n' +
      "event: done\ndata: {}\n\n"
    // Cuts inside an event name, inside a two-byte character, between the two newlines and inside the JSON.
    const raw = new TextEncoder().encode(wire)
    const byteAt = (s: string) => new TextEncoder().encode(wire.slice(0, wire.indexOf(s))).length
    const cuts = [3, byteAt("é") + 1, byteAt("\n\n") + 1, 90, raw.length - 3, raw.length]
    expect(cuts).toEqual([...cuts].sort((a, b) => a - b))
    const parts: Uint8Array[] = []
    cuts.reduce((from, to) => (parts.push(raw.slice(from, to)), to), 0)
    const fetchMock = vi.fn(async () => {
      const body = new ReadableStream<Uint8Array>({
        start(c) {
          for (const p of parts) c.enqueue(p)
          c.close()
        },
      })
      return new Response(body, { status: 200 })
    })
    vi.stubGlobal("fetch", fetchMock)

    const events = await collect((on) => ask("Test question?", on))

    expect(fetchMock).toHaveBeenCalledWith("api/ask", expect.objectContaining({
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question: "Test question?" }),
    }))
    expect(events.map((e) => e.name)).toEqual(["text", "text", "request", "rows", "done"])
    expect(texts(events)).toBe("Test é fixture")
    expect(events[3]).toEqual({ name: "rows", data: { columns: ["value"], rows: [[1.5]] } })
  })

  it("passes a server error event through to done", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => sseResponse([
      'event: error\ndata: {"message": "Set ANTHROPIC_API_KEY."}\n\nevent: done\ndata: {}\n\n',
    ])))
    const events = await collect((on) => ask("q", on))
    expect(events).toEqual([
      { name: "error", data: { message: "Set ANTHROPIC_API_KEY." } },
      { name: "done", data: {} },
    ])
  })

  it("turns a 415 reply into one error event and done", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(
      JSON.stringify({ error: "Send the question as application/json." }),
      { status: 415, headers: { "Content-Type": "application/json" } },
    )))
    const events = await collect((on) => ask("q", on))
    expect(events).toEqual([
      { name: "error", data: { message: "Send the question as application/json." } },
      { name: "done", data: {} },
    ])
  })

  it("turns a plain-text 400 reply into one error event and done", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response("Invalid host header", { status: 400 })))
    const events = await collect((on) => ask("q", on))
    expect(events.map((e) => e.name)).toEqual(["error", "done"])
  })

  it("turns a network failure into one error event and done", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => { throw new TypeError("fetch failed") }))
    const events = await collect((on) => ask("q", on))
    expect(events.map((e) => e.name)).toEqual(["error", "done"])
  })

  it("ends a stream cut before done with one error event and done", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => sseResponse(['event: text\ndata: {"delta": "Test"}\n\nevent: te'])))
    const events = await collect((on) => ask("q", on))
    expect(events.map((e) => e.name)).toEqual(["text", "error", "done"])
  })
})
