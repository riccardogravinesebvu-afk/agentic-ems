// The demo server's events (Task 15 contract), and the same events replayed from a saved sample.

export type Cell = string | number | null
export type Row = Cell[]
// The model's query_metric arguments as the layer accepted them: only metric, start and end are sure to be there.
export type MetricRequest = { metric?: string; start?: string; end?: string; [field: string]: unknown }

export type Sample = {
  question: string
  text: string
  request: MetricRequest | null
  sql: string | null
  columns: string[] | null
  rows: Row[] | null
  refusal: string | null
}

export type StreamEvent =
  | { name: "text"; data: { delta: string } }
  | { name: "request"; data: { request: MetricRequest; sql: string } }
  | { name: "rows"; data: { columns: string[]; rows: Row[] } }
  | { name: "refusal"; data: { reason: string } }
  | { name: "error"; data: { message: string } }
  | { name: "done"; data: Record<string, never> }

type OnEvent = (event: StreamEvent) => void

function isSample(s: Sample | null): boolean {
  if (typeof s?.text !== "string") return false
  if (s.refusal !== null) return typeof s.refusal === "string"
  return (
    typeof s.request === "object" && s.request !== null && typeof s.sql === "string" &&
    Array.isArray(s.columns) && Array.isArray(s.rows)
  )
}

/** Replays a saved sample as the server would stream it, one word per tick; an abort clears the pending tick. */
export async function replay(sample: Sample, onEvent: OnEvent, delayMs: number, signal?: AbortSignal): Promise<void> {
  if (!isSample(sample)) {
    onEvent({ name: "error", data: { message: "This recorded answer is malformed." } })
    onEvent({ name: "done", data: {} })
    return
  }
  let timer: ReturnType<typeof setTimeout> | undefined
  signal?.addEventListener("abort", () => clearTimeout(timer), { once: true })
  // An aborted replay waits on a tick that never comes, so it emits nothing more.
  const tick = () => new Promise((resolve) => (timer = signal?.aborted ? undefined : setTimeout(resolve, delayMs)))
  for (const delta of sample.text.match(/\S+\s*|\s+/g) ?? []) {
    await tick()
    onEvent({ name: "text", data: { delta } })
  }
  await tick()
  if (sample.refusal !== null) {
    onEvent({ name: "refusal", data: { reason: sample.refusal } })
  } else {
    onEvent({ name: "request", data: { request: sample.request!, sql: sample.sql! } })
    onEvent({ name: "rows", data: { columns: sample.columns!, rows: sample.rows! } })
  }
  onEvent({ name: "done", data: {} })
}

/** Asks the local demo server; any failure becomes one error event, and done always comes last. */
export async function ask(question: string, onEvent: OnEvent, signal?: AbortSignal): Promise<void> {
  const fail = (message: string) => {
    onEvent({ name: "error", data: { message } })
    onEvent({ name: "done", data: {} })
  }
  let response: Response
  try {
    response = await fetch("api/ask", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question }),
      signal,
    })
  } catch {
    return fail("Could not reach the demo server.")
  }
  if (!response.ok || !response.body) {
    const body = await response.json().catch(() => null)
    return fail(typeof body?.error === "string" ? body.error : `The demo server answered with status ${response.status}.`)
  }
  let done = false
  const parse = eventParser((event) => {
    done ||= event.name === "done"
    onEvent(event)
  })
  const reader = response.body.pipeThrough(new TextDecoderStream()).getReader()
  try {
    for (let chunk = await reader.read(); !chunk.done && !done; chunk = await reader.read()) parse(chunk.value)
  } catch {
    await reader.cancel().catch(() => {}) // stops the download; the server ends its run when the client goes
    if (!done) return fail("The answer stopped before it ended.")
  }
  if (!done) fail("The answer stopped before it ended.")
}

/** Server-sent events from text chunks that may cut an event anywhere; the server writes "\n" line ends only.
 * Comment lines and blocks with no data are skipped. */
function eventParser(onEvent: OnEvent): (chunk: string) => void {
  let buffer = ""
  return (chunk) => {
    buffer += chunk
    for (let end = buffer.indexOf("\n\n"); end >= 0; end = buffer.indexOf("\n\n")) {
      const block = buffer.slice(0, end)
      buffer = buffer.slice(end + 2)
      let name = ""
      let data: string | null = null
      for (const line of block.split("\n")) {
        if (line.startsWith("event:")) name = line.slice(6).trim()
        else if (line.startsWith("data:")) data = (data ?? "") + line.slice(5).trim()
      }
      if (data !== null) onEvent({ name, data: JSON.parse(data) } as StreamEvent)
    }
  }
}

/** A line for a time series, bars for a breakdown, nothing for a single value. */
export function chartKind(columns: string[], rows: Row[]): "line" | "bar" | "none" {
  if (rows.length === 0) return "none"
  if (columns.includes("bucket_start")) return "line"
  if (columns.includes("meter_id") || columns.includes("tariff_band")) return "bar"
  return "none"
}

const UNITS: Record<string, string> = {
  consumption_kwh: "kWh",
  peak_kw: "kW",
  cost_eur: "EUR",
  energy_per_unit: "kWh per unit",
}

export function unitOf(metric: unknown): string {
  return (typeof metric === "string" && UNITS[metric]) || ""
}

export function formatValue(value: Cell, unit: string): string {
  if (typeof value !== "number") return "no value"
  const number = value.toLocaleString("en-US", { maximumFractionDigits: Math.abs(value) >= 1000 ? 0 : 1 })
  return unit ? `${number} ${unit}` : number
}

/** A bucket from its own string, read as plant local time and never shifted: "2026-03-29T03:00:00+02:00" is "2026-03-29 03:00". */
export function formatBucket(bucket: string): string {
  const match = /^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2}):(\d{2})/.exec(bucket)
  if (!match) return bucket
  const [, date, time, seconds] = match
  return time === "00:00" && seconds === "00" ? date : `${date} ${time}`
}

/** A key cell as the page shows it. */
export function formatKey(column: string, cell: Cell): string {
  return column === "bucket_start" && typeof cell === "string" ? formatBucket(cell) : String(cell)
}

/** The number the page leads with: the only value, or the highest one and the keys of its row. */
export function headline(columns: string[], rows: Row[]): { value: Cell; label: string | null } | null {
  const at = columns.indexOf("value")
  const keys = columns.map((_, i) => i).filter((i) => i !== at)
  const rank = (row: Row) => (typeof row[at] === "number" ? (row[at] as number) : -Infinity)
  const top = rows.reduce<Row | null>((best, row) => (best === null || rank(row) > rank(best) ? row : best), null)
  if (top === null) return null
  const label = keys.length ? keys.map((i) => formatKey(columns[i], top[i])).join(", ") : null
  if (rows.length === 1) return { value: top[at], label }
  return { value: top[at], label: label !== null && typeof top[at] === "number" ? `highest, ${label}` : null }
}

/** Chart points: one per x value (time in order), one series per combination of the other key columns. */
export function pivot(columns: string[], rows: Row[]) {
  const at = columns.indexOf("value")
  const xName = ["bucket_start", "meter_id", "tariff_band"].find((c) => columns.includes(c))!
  const x = columns.indexOf(xName)
  const others = columns.map((_, i) => i).filter((i) => i !== at && i !== x)
  const instant = (row: Row) => Date.parse(String(row[x]))
  const ordered =
    xName === "bucket_start"
      ? [...rows].sort((a, b) => instant(a) - instant(b) || String(a[x]).localeCompare(String(b[x])))
      : rows
  const series: string[] = []
  const points = new Map<string, Record<string, Cell>>() // keyed by the raw bucket: the repeated autumn hour stays two points
  for (const row of ordered) {
    const name = others.length ? others.map((i) => formatKey(columns[i], row[i])).join(", ") : "value"
    if (!series.includes(name)) series.push(name)
    const point = points.get(String(row[x])) ?? { x: formatKey(xName, row[x]) }
    point[`s${series.indexOf(name)}`] = row[at]
    points.set(String(row[x]), point)
  }
  return { xName, data: [...points.values()], series }
}
