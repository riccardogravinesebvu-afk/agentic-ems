// Test fixtures only: the numbers are made up, and the built page never imports this file.
import type { Sample } from "./stream"

export const seriesSample: Sample = {
  question: "TEST FIXTURE: a daily series",
  text: "Test fixture text.\n\nIt has two turns,  double spaces and a trailing space ",
  request: { metric: "consumption_kwh", start: "2001-01-01", end: "2001-01-04", grain: "day" },
  sql: "SELECT 1 AS test_fixture",
  columns: ["bucket_start", "value"],
  rows: [["2001-01-01", 111.1], ["2001-01-02", 222.2], ["2001-01-03", 333.3]],
  refusal: null,
}

export const breakdownSample: Sample = {
  question: "TEST FIXTURE: a breakdown by meter",
  text: "Test fixture breakdown.",
  request: { metric: "cost_eur", node: "test_node", breakdown: "line", start: "2001-01-01", end: "2001-02-01" },
  sql: "SELECT 2 AS test_fixture",
  columns: ["meter_id", "value"],
  rows: [["test_meter_a", 12.5], ["test_meter_b", 99999.9]],
  refusal: null,
}

export const singleSample: Sample = {
  question: "TEST FIXTURE: a single value",
  text: "Test fixture single value.",
  request: { metric: "peak_kw", start: "2001-01-01", end: "2001-01-08" },
  sql: "SELECT 3 AS test_fixture",
  columns: ["value"],
  rows: [[1234.5678]],
  refusal: null,
}

export const refusalSample: Sample = {
  question: "TEST FIXTURE: out of scope",
  text: "Test fixture refusal text.",
  request: null,
  sql: null,
  columns: null,
  rows: null,
  refusal: "Test fixture reason.",
}
