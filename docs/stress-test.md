# Stress test: twelve harder questions

Run on 2026-11-12, after the benchmark. Twelve questions written to be hard rather than scoreable
(`results/stress/questions.toml`), one run each on the `semantic` arm and on the `sql` arm, with the model,
effort and prompts of the recorded benchmark runs. 24 runs, 0.67 EUR. The runs are in `results/stress/runs/`.

This is not a benchmark. One run per question, no scorer, no interval: it shows where things break, not how
often. Nothing here enters the README's results. The answers were read by hand. Where both arms returned a
value they were compared with each other, and S5 and S9 were recomputed from the database.

## What happened

| | Question | `semantic` | `sql` |
|---|---|---|---|
| S1 | Cost of the four departments, month by month | right, one request | right |
| S2 | Plant consumption by tariff band, each day of April | right rows; text covers 1 to 20 April only | same |
| S3 | Share of plant energy no department meter measured | right in the text (4.96%); rows are the site total only | right, share in the rows |
| S4 | Hourly consumption on the clock-change day | right, 25 hours, repeated hour kept as two | right |
| S5 | Peak demand in the first week of each of six months | **no answer**: four requests used, no text | right, six rows |
| S6 | Painting's daily cost by tariff band, August | right rows; text covers 1 to 20 August only | same |
| S7 | Welding machine that changed most, Q1 to Q3 | right in the text; rows are Q3 only | right, change in the rows |
| S8 | Load factor in July | right in the text (0.478); rows are the peak only | right, factor in the rows |
| S9 | Share of consumption the solar array covered | refused, with the reason (generation is outside the metrics) | answered (5.1%) |
| S10 | Departments ranked by average price per kWh | right in the text; rows are consumption only | right, ranking in the rows |
| S11 | 15-minute load of Press 5 on its heaviest March day | right day and rows; text covers the morning only | same |
| S12 | The same kind of question asked in Italian | right, answered in Italian | right |

No run returned a wrong value. Every value the two arms both gave agrees.

## What broke

1. **A run can end with rows and no answer (S5).** The question needs six periods and a request holds one.
   The model spent its four requests and the loop ended on the fourth result, with no turn left to write
   anything. The run still counted as "rows". On the page this was a chart and a headline number with no
   text, for a question they do not answer.
2. **The model sees only the first 50 rows of a result (S2, S6, S11, both arms).** The page and the rows are
   complete; the text describes two thirds of the month, and says so. In S5 this limit is also what pushed
   the model into asking again.
3. **When the answer takes several requests, the rows are only the last one (S3, S7, S8, S10).** The text
   was right every time, but the page's headline number was an ingredient: 692,423 kWh for a question about
   a share, 2,025.6 kW for a load factor. This is the benchmark's "ingredients, not answers" finding, seen in
   the page.
4. **One request holds one period and one of four metrics.** S5, S7, S9 and S10 are ordinary questions in
   this domain, and each is outside what a single request can say. The SQL arm answered all four in one query.

## What changed because of it

- The page now says when the model made several requests ("the request, chart and rows shown are the last
  one only") and no longer shows a headline number then. It says so too when a run used all its requests
  before writing an answer.
- Tariff bands over time are drawn as a stacked area (S2, S6). This was added just before the run.

## What did not change, and why

- **No other chart type.** Lines, bars and the stacked area drew every result the semantic arm returned.
  The answers that would want another chart (six bars for S5, two bars per machine for S7) are ones the
  layer cannot return in one request, so a new chart would have nothing to draw.
- **The agent loop and the 50-row limit.** Giving the model a closing turn at the limit, or showing it more
  rows, changes the agent the benchmark measured. That is a decision for the owner, with a rerun or a clear
  note next to the recorded numbers.
- **The request language.** Several periods in one request and ratios of two metrics are the real gap. They
  are the next piece of work, not a fix.
