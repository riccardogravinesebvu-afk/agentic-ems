import { useEffect, useRef, useState, type FormEvent } from "react"
import { Area, AreaChart, Bar, BarChart, CartesianGrid, Line, LineChart, XAxis, YAxis } from "recharts"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import {
  ChartContainer,
  ChartLegend,
  ChartLegendContent,
  ChartTooltip,
  ChartTooltipContent,
  type ChartConfig,
} from "@/components/ui/chart"
import { Input } from "@/components/ui/input"
import {
  ask,
  chartKind,
  formatValue,
  headline,
  keyFormatter,
  pivot,
  replay,
  textBlocks,
  unitOf,
  type MetricRequest,
  type Row,
  type Sample,
  type StreamEvent,
} from "./stream"

type Mode = "checking" | "local" | "nokey" | "static"
type Entry = { file: string; question: string }
type View = {
  question: string
  text: string
  request: MetricRequest | null
  sql: string | null
  columns: string[] | null
  rows: Row[] | null
  refusal: string | null
  error: string | null
  streaming: boolean
}

const EMPTY: View = {
  question: "",
  text: "",
  request: null,
  sql: null,
  columns: null,
  rows: null,
  refusal: null,
  error: null,
  streaming: false,
}
const MAX_QUESTION_CHARS = 500 // the server's limit, MAX_QUESTION_CHARS in src/agentic_ems/demo.py
const REPLAY_DELAY_MS = 25

function apply(view: View, event: StreamEvent): View {
  switch (event.name) {
    case "text":
      return { ...view, text: view.text + event.data.delta }
    case "request":
      return { ...view, request: event.data.request, sql: event.data.sql }
    case "rows":
      return { ...view, columns: event.data.columns, rows: event.data.rows }
    case "refusal":
      return { ...view, refusal: event.data.reason }
    case "error":
      return { ...view, error: event.data.message }
    case "done":
      return { ...view, streaming: false }
    default:
      return view
  }
}

function isIndex(value: unknown): value is Entry[] {
  return (
    Array.isArray(value) &&
    value.every((e) => typeof e?.file === "string" && typeof e?.question === "string")
  )
}

/** `**x**` in the model's text as bold; everything else stays plain text. */
function bold(text: string) {
  return text.split(/\*\*(.+?)\*\*/).map((part, i) => (i % 2 ? <strong key={i}>{part}</strong> : part))
}

export default function App() {
  const [mode, setMode] = useState<Mode>("checking")
  const [samples, setSamples] = useState<Entry[] | null>(null)
  const [samplesMissing, setSamplesMissing] = useState(false)
  const [draft, setDraft] = useState("")
  const [view, setView] = useState<View>(EMPTY)
  const runId = useRef(0)
  const running = useRef<AbortController | null>(null)

  useEffect(() => {
    fetch("api/health")
      .then((r) => (r.ok ? r.json() : null))
      .then((h) => setMode(h?.ok !== true ? "static" : h.has_key ? "local" : "nokey"))
      .catch(() => setMode("static"))
    fetch("samples/index.json")
      .then((r) => (r.ok ? r.json() : null))
      .then((index) => (isIndex(index) && index.length ? setSamples(index) : setSamplesMissing(true)))
      .catch(() => setSamplesMissing(true))
  }, [])

  // One run at a time: a new run cancels the one in flight and ignores its late events.
  function start(question: string, go: (onEvent: (e: StreamEvent) => void, signal: AbortSignal) => Promise<void>) {
    running.current?.abort()
    const controller = new AbortController()
    running.current = controller
    const id = ++runId.current
    setView({ ...EMPTY, question, streaming: true })
    go((event) => {
      if (runId.current === id) setView((v) => apply(v, event))
    }, controller.signal)
  }

  function playSample(entry: Entry) {
    start(entry.question, async (onEvent, signal) => {
      let sample: Sample
      try {
        const response = await fetch(`samples/${entry.file}`)
        if (!response.ok) throw new Error()
        sample = await response.json()
      } catch {
        onEvent({ name: "error", data: { message: "This recorded answer could not be loaded." } })
        onEvent({ name: "done", data: {} })
        return
      }
      await replay(sample, onEvent, REPLAY_DELAY_MS, signal)
    })
  }

  function submit(e: FormEvent) {
    e.preventDefault()
    const question = draft.trim()
    if (question) start(question, (onEvent, signal) => ask(question, onEvent, signal))
  }

  const freeText = mode === "local"
  const unit = unitOf(view.request?.metric)
  const answered = view.rows !== null && view.columns !== null && view.error === null
  const top = answered ? headline(view.columns!, view.rows!) : null
  const kind = answered ? chartKind(view.columns!, view.rows!) : "none"

  return (
    <main className="mx-auto flex max-w-6xl flex-col gap-6 px-4 py-8">
      <header className="flex flex-col gap-1">
        <h1 className="text-xl font-semibold">agentic-ems</h1>
        <p className="text-sm text-muted-foreground">
          Ask a question about a synthetic plant's energy data. The model fills a request. Code writes the SQL.
        </p>
      </header>

      <section className="flex flex-col gap-3" aria-label="Question">
        <form onSubmit={submit} className="flex flex-col gap-2">
          <label htmlFor="question" className="text-sm font-medium">
            Your question
          </label>
          <div className="flex gap-2">
            <Input
              id="question"
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              maxLength={MAX_QUESTION_CHARS}
              disabled={!freeText}
              aria-describedby={mode === "static" || mode === "nokey" ? "question-note" : undefined}
              className="h-10"
            />
            <Button type="submit" className="h-10 px-4" disabled={!freeText || view.streaming || !draft.trim()}>
              Ask
            </Button>
          </div>
          {mode === "static" && (
            <p id="question-note" className="text-sm text-muted-foreground">
              Recorded answers. For your own questions, clone the repository and run{" "}
              <code className="font-mono text-foreground">uv run python -m agentic_ems.demo serve</code>.
            </p>
          )}
          {mode === "nokey" && (
            <p id="question-note" className="text-sm text-muted-foreground">
              Set <code className="font-mono text-foreground">ANTHROPIC_API_KEY</code> in the server's environment to
              ask your own question. The recorded answers still work.
            </p>
          )}
        </form>
        <div className="flex flex-col gap-2">
          <h2 className="text-sm font-medium">Recorded answers</h2>
          {samplesMissing ? (
            <p className="text-sm text-muted-foreground">No recorded answers in this build.</p>
          ) : (
            <div className="flex flex-wrap gap-2">
              {samples?.map((entry) => (
                <Button
                  key={entry.file}
                  type="button"
                  variant="outline"
                  className="h-auto max-w-full py-1.5 text-left whitespace-normal"
                  onClick={() => playSample(entry)}
                >
                  {entry.question}
                </Button>
              ))}
            </div>
          )}
        </div>
      </section>

      <div className="grid items-start gap-6 md:grid-cols-[3fr_2fr]">
        <div className="flex min-w-0 flex-col gap-6">
          <Card>
            <CardHeader>
              <CardTitle>
                <h2>Answer</h2>
              </CardTitle>
            </CardHeader>
            <CardContent>
              <div aria-live="polite" aria-busy={view.streaming} className="flex flex-col gap-3">
                {view.question === "" && (
                  <p className="text-sm text-muted-foreground">Pick a recorded answer or ask your own question.</p>
                )}
                {view.question !== "" && <p className="text-sm text-muted-foreground">{view.question}</p>}
                {top && (
                  <p>
                    <span className="text-3xl font-semibold tabular-nums">{formatValue(top.value, unit)}</span>
                    {top.label && <span className="ml-2 text-sm text-muted-foreground">{top.label}</span>}
                  </p>
                )}
                {answered && view.rows!.length === 0 && <p className="font-medium">No rows for this request.</p>}
                {view.refusal !== null && <p className="font-medium">Out of scope: {view.refusal}</p>}
                {view.error !== null && <p className="font-medium text-destructive">Error: {view.error}</p>}
                {textBlocks(view.text).map((block, i) =>
                  block.kind === "text" ? (
                    <p key={i} className="whitespace-pre-wrap">{bold(block.text)}</p>
                  ) : (
                    <div key={i} className="overflow-x-auto">
                      <table className="text-sm tabular-nums">
                        <thead>
                          <tr>{block.header.map((cell, c) => <th key={c} scope="col" className="border-b px-2 py-1 text-left font-medium">{bold(cell)}</th>)}</tr>
                        </thead>
                        <tbody>
                          {block.rows.map((row, r) => (
                            <tr key={r}>{row.map((cell, c) => <td key={c} className="border-b px-2 py-1">{bold(cell)}</td>)}</tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  ),
                )}
                {view.streaming && <p className="text-sm text-muted-foreground">Streaming...</p>}
              </div>
            </CardContent>
          </Card>

          {kind !== "none" && <Chart kind={kind} columns={view.columns!} rows={view.rows!} unit={unit} metric={view.request?.metric} />}
        </div>

        <Card className="min-w-0">
          <CardHeader>
            <CardTitle>
              <h2>Structured request</h2>
            </CardTitle>
          </CardHeader>
          <CardContent className="flex flex-col gap-4">
            <div className="flex flex-col gap-1">
              <h3 className="text-sm text-muted-foreground">Written by the model</h3>
              {view.request ? (
                <pre className="font-mono text-sm whitespace-pre-wrap wrap-anywhere">
                  {JSON.stringify(view.request, null, 2)}
                </pre>
              ) : (
                <p className="text-sm text-muted-foreground">None for this answer.</p>
              )}
            </div>
            <div className="flex flex-col gap-1">
              <h3 className="text-sm text-muted-foreground">SQL, written by the compiler, not the model</h3>
              {view.sql ? (
                <pre className="font-mono text-sm whitespace-pre-wrap wrap-anywhere">{view.sql}</pre>
              ) : (
                <p className="text-sm text-muted-foreground">None for this answer.</p>
              )}
            </div>
          </CardContent>
        </Card>
      </div>
    </main>
  )
}

function Chart(props: { kind: "line" | "area" | "bar"; columns: string[]; rows: Row[]; unit: string; metric: unknown }) {
  const { kind, columns, rows, unit } = props
  const { xName, data: points, series } = pivot(columns, rows)
  // A band with no row on a day used nothing that day (a Sunday has one band): 0, so the stack has no hole.
  const data = kind === "area" ? points.map((p) => ({ ...Object.fromEntries(series.map((_, i) => [`s${i}`, 0])), ...p })) : points
  const formatKey = keyFormatter(columns, rows)
  const metric = typeof props.metric === "string" ? props.metric : "value"
  // shortcut: four colours that read on white (chart-1 is too light), repeated past four series; upgrade if a sample needs more.
  const config: ChartConfig = Object.fromEntries(
    series.map((name, i) => [`s${i}`, { label: series.length > 1 ? name : metric, color: `var(--chart-${2 + (i % 4)})` }]),
  )
  const keys = series.map((_, i) => `s${i}`)
  const axes = (
    <>
      <CartesianGrid vertical={false} />
      <XAxis dataKey="x" tickLine={false} axisLine={false} tickMargin={8} minTickGap={16} />
      <YAxis tickLine={false} axisLine={false} width={72} tickFormatter={(v: number) => formatValue(v, "")} />
      <ChartTooltip content={<ChartTooltipContent valueFormatter={(v) => formatValue(v, unit)} />} />
      {series.length > 1 && <ChartLegend content={<ChartLegendContent />} />}
    </>
  )
  const summary = `${{ line: "Line", area: "Stacked area", bar: "Bar" }[kind]} chart of ${metric}${unit ? ` in ${unit}` : ""} by ${xName}, ${rows.length} rows.`

  return (
    <Card>
      <CardHeader>
        <CardTitle>
          <h2>Chart</h2>
        </CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        <figure className="flex flex-col gap-2">
          <ChartContainer config={config} className="aspect-auto h-64 w-full">
            {kind === "line" ? (
              <LineChart accessibilityLayer data={data}>
                {axes}
                {keys.map((k) => (
                  <Line
                    key={k}
                    dataKey={k}
                    type="linear"
                    stroke={`var(--color-${k})`}
                    strokeWidth={2}
                    dot={data.length === 1}
                    isAnimationActive={false}
                  />
                ))}
              </LineChart>
            ) : kind === "area" ? (
              <AreaChart accessibilityLayer data={data}>
                {axes}
                {keys.map((k) => (
                  <Area
                    key={k}
                    dataKey={k}
                    type="linear"
                    stackId="total"
                    stroke={`var(--color-${k})`}
                    fill={`var(--color-${k})`}
                    fillOpacity={0.6}
                    isAnimationActive={false}
                  />
                ))}
              </AreaChart>
            ) : (
              <BarChart accessibilityLayer data={data}>
                {axes}
                {keys.map((k) => (
                  <Bar key={k} dataKey={k} fill={`var(--color-${k})`} radius={4} isAnimationActive={false} />
                ))}
              </BarChart>
            )}
          </ChartContainer>
          <figcaption className="text-sm text-muted-foreground">{summary} The same rows are in the table below.</figcaption>
        </figure>
        <details>
          <summary className="cursor-pointer text-sm font-medium">Rows as a table</summary>
          <div className="mt-2 max-h-80 overflow-auto">
            <table className="w-full text-sm">
              <thead>
                <tr>
                  {columns.map((c) => (
                    <th key={c} scope="col" className="border-b py-1 pr-4 text-left font-medium">
                      {c}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {rows.map((row, r) => (
                  <tr key={r}>
                    {row.map((cell, i) => (
                      <td key={i} className="border-b py-1 pr-4 tabular-nums">
                        {columns[i] === "value" ? formatValue(cell, unit) : formatKey(columns[i], cell)}
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </details>
      </CardContent>
    </Card>
  )
}
