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
  requests?: number // absent in samples saved before the field existed: one request
}

export type StreamEvent =
  | { name: "text"; data: { delta: string } }
  | { name: "note"; data: { requests: number } }
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
    if ((sample.requests ?? 1) > 1)
      onEvent({ name: "note", data: { requests: sample.requests ?? 1 } })
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
export function chartKind(columns: string[], rows: Row[]): "line" | "area" | "bar" | "none" {
  if (rows.length === 0) return "none"
  // Tariff bands split one total over time, so they stack to it. Meters are never stacked: a parent holds its children.
  if (columns.includes("bucket_start") && columns.includes("tariff_band") && !columns.includes("meter_id")) {
    const at = columns.indexOf("bucket_start")
    return new Set(rows.map((row) => row[at])).size > 1 ? "area" : "bar" // one bucket has no area to draw
  }
  if (columns.includes("bucket_start")) return "line"
  if (columns.includes("meter_id") || columns.includes("tariff_band")) return "bar"
  return "none"
}

const UNITS: Record<string, string> = {
  consumption_kwh: "kWh",
  peak_kw: "kW",
  cost_eur: "EUR",
  energy_per_unit: "kWh/unit",
}

export function unitOf(metric: unknown): string {
  return (typeof metric === "string" && UNITS[metric]) || ""
}

export function formatValue(value: Cell, unit: string): string {
  if (typeof value !== "number") return "no value"
  const size = Math.abs(value)
  // At one decimal a non-zero value below 0.05 would show as 0, so it keeps two significant digits.
  const digits = size >= 1000 ? { maximumFractionDigits: 0 } : size >= 0.05 || size === 0 ? { maximumFractionDigits: 1 } : { maximumSignificantDigits: 2 }
  const number = value.toLocaleString("en-US", digits)
  return unit ? `${number} ${unit}` : number
}

/** A bucket from its own string, read as plant local time and never shifted: "2026-03-29T03:00:00+02:00" is "2026-03-29 03:00". */
export function formatBucket(bucket: string): string {
  const match = /^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2}):(\d{2})/.exec(bucket)
  if (!match) return bucket
  const [, date, time, seconds] = match
  return time === "00:00" && seconds === "00" ? date : `${date} ${time}`
}

/** Key cells as the page shows them; buckets of these rows that share a short label, such as the two autumn
 * 02:00 hours, keep their UTC offset: "2026-10-25 02:00 +02:00" and "2026-10-25 02:00 +01:00". */
export function keyFormatter(columns: string[], rows: Row[]): (column: string, cell: Cell) => string {
  const at = columns.indexOf("bucket_start")
  const buckets = new Map<string, Set<string>>() // short label: the distinct bucket strings that have it
  for (const row of rows) {
    const bucket = String(row[at])
    const label = formatBucket(bucket)
    buckets.set(label, (buckets.get(label) ?? new Set()).add(bucket))
  }
  return (column, cell) => {
    if (column !== "bucket_start" || typeof cell !== "string") return String(cell)
    const label = formatBucket(cell)
    return (buckets.get(label)?.size ?? 0) > 1 ? `${label} ${cell.slice(19)}` : label
  }
}

/** The number the page leads with: the only value, or the highest one and the keys of its row. */
export function headline(columns: string[], rows: Row[]): { value: Cell; label: string | null } | null {
  const at = columns.indexOf("value")
  const keys = columns.map((_, i) => i).filter((i) => i !== at)
  const rank = (row: Row) => (typeof row[at] === "number" ? (row[at] as number) : -Infinity)
  const top = rows.reduce<Row | null>((best, row) => (best === null || rank(row) > rank(best) ? row : best), null)
  if (top === null) return null
  const formatKey = keyFormatter(columns, rows)
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
  const formatKey = keyFormatter(columns, rows)
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

export type TextBlock = { kind: "text"; text: string } | { kind: "table"; header: string[]; rows: string[][] }

// shortcut: a pipe inside a code span still splits the cell; use a Markdown parser if answers start to hold them.
const cells = (line: string) =>
  line.trim().replace(/^\||(?<!\\)\|$/g, "").split(/(?<!\\)\|/).map((cell) => cell.trim().replaceAll("\\|", "|"))

/** The model's text cut into paragraphs and Markdown tables; a table counts once its separator line has arrived. */
export function textBlocks(text: string): TextBlock[] {
  const lines = text.split("\n")
  const blocks: TextBlock[] = []
  let plain: string[] = []
  const flush = () => {
    const joined = plain.join("\n").trim()
    if (joined) blocks.push({ kind: "text", text: joined })
    plain = []
  }
  for (let i = 0; i < lines.length; i++) {
    if (lines[i].startsWith("|") && /^\|(?=.*-)[\s:|-]+\|\s*$/.test(lines[i + 1] ?? "")) {
      flush()
      const header = cells(lines[i])
      const rows: string[][] = []
      for (i += 2; i < lines.length && lines[i].startsWith("|"); i++) rows.push(cells(lines[i]))
      i--
      blocks.push({ kind: "table", header, rows })
    } else plain.push(lines[i])
  }
  flush()
  return blocks
}
