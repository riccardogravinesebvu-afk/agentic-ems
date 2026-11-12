# Review log

## 2026-11-11

- The owner read the draft list of question types, not the final wording of the questions.
  He approved proceeding with those types without striking rows.
- Eight question types were added from a list of generic error classes prepared in a separate
  session and reviewed by the owner.
- The wording of each question was written by an agent to resemble how plant staff ask and has
  not yet been reworded by the owner.
- The controller made these decisions under the owner's standing instruction to take the most
  rigorous option:
  - H19's correct answer is the department's own meter alone.
  - O7, O8, O12 (later rejected, below) and O13 ask for a single value (one product, weekends only, the average per
    day), so their answers have an honest key.
  - M4 asks for one whole day at 15-minute grain, because a request period is whole dates.
  - The question that adds the solar array's production to the plant's consumption (draft H21)
    was first moved to unanswerable, then rejected after the second reading below: the sum is
    plain arithmetic on two quantities in the data, so a refusal as the only correct outcome is
    disputable. The unanswerable group has 8 questions.
- A second reader reviewed the whole set on the same day. As a result the asks of H2, H6, H11,
  H17, C6, M3, M4, M5, O2, O3, O10 and O13 were reworded, M3 moved to a line with a night
  shift, and the notes of H10, O7 and O11 were corrected.
  Confirmed on the data: for H10 the coincident peak is clearly below the sum of the separate
  peaks; for O11 the threshold is crossed during many hours; for M3 night-shift orders cross
  midnight in the chosen week.
- A second reader reviewed the hand-written reference queries on the same day. After that
  review the controller rejected O12 (energy per hood): a second defensible reading, both
  Painting lines' kWh whenever a Hood order runs on either line, differs from the reference by
  about a third, and the question is constructed rather than one plant staff would ask. The id
  O12 is not reused. The out-of-scope group has 13 questions. The same review added a
  `children_sum` wrong answer to H19 (the two Painting lines summed).
- The writer of the second computation (`reference_py.py`) read the controller's progress
  notes, which contained two figures from a reviewer: one for the two-line peak question and
  one for the threshold-hours question. The writer states it did not use them. A reviewer
  comparing the two computations found the second one written from the rules by a different
  method.

## 2026-11-12

- Owner's check of a sample of answers. The owner was shown ten questions with their reference
  values and known wrong values: H1, H2, H8, H10, H13, H19, C5, M1, O1 and O9. The owner
  accepted all ten and raised no objection, including the two rules called out as open to
  dispute: a parent meter's own reading is the answer where its children sum to less (H1, H2,
  H13), and a department plus one of its own lines is the department's reading (H19). The
  owner read the values and the reading of each question; the owner did not recompute them.

- Baseline pilot (paid). The owner approved a pilot of the text-to-SQL arm with a cap of 2 EUR.
  Eight hierarchy questions (H1, H2, H5, H8, H10, H13, H16, H19), one repeat, `claude-opus-5-5`,
  effort medium, 16,000 output tokens per response, 1.00 EUR per USD. It ran as two invocations
  (six questions, then two) because the spend ledger reserves each attempt at its worst-case price.
  - Outcomes: 8 correct, 0 known wrong, 0 other wrong, 0 refusals, 0 no answer. No run double
    counted, summed children or added separate peaks.
  - Each run made one `run_sql` call. The `sqlglot` check refused no query, so none that DuckDB
    would accept was refused.
  - Spend: 0.12 EUR in total, 0.015 EUR per run on average. No retry, no unscored attempt.
  - Caching: the first response wrote 6,850 tokens to the cache (`cache_creation_input_tokens`);
    every later response read them.
  - Not done before the pilot: the plan asked the owner to read `docs/schema.md` first. The owner
    approved the pilot without confirming that reading.
  - Eight questions and one repeat say nothing about the rate of wrong answers; the full run
    reads all 55 questions.

- `docs/schema.md`. After the pilot and before the full run the owner asked to wait and read the
  document. It was shown to the owner in full, unchanged since the pilot; the owner answered
  "Ok" and raised no objection. The controller pointed out that rules R2, R3 and R5 hand the
  text-to-SQL arm the method of each answer, and that the document would stay as it was fixed
  before the pilot rather than be changed after seeing a result.

- Full benchmark run (paid), run id `20261112T135733Z`. Preregistered before the run: 55
  questions, both arms, 5 repeats, minimum 3, cap 12 EUR; a difference between arms is claimed
  only when its 95% interval excludes 0 and it is at least 5 points. A three-question pilot of
  both arms (H5, M1, U1; 0.11 EUR) priced the semantic arm first.
  - All 5 repeats completed: 550 runs, 6.82 EUR, no retry, no error, no unscored attempt.
  - Text-to-SQL arm: 235 of 235 answerable runs correct, 40 of 40 refusals on unanswerable
    questions. No wrong value.
  - Semantic arm: 169 of 170 correct on hierarchy, control and metric definition (one refusal
    on H19), 40 of 40 refusals on unanswerable questions. On the 13 out-of-scope questions, 0 of
    65 correct: 35 refusals (O1, O2, O3, O6, O7, O11, O14) and 30 runs scored `other_wrong`
    (O4, O5, O8, O9, O10, O13, each in all five repeats).
  - The controller read the 30 `other_wrong` transcripts after the run. In each, the rows of
    the last query were correct ingredients (monthly totals, daily values, all machines), and
    the model's text stated the reference value, computed by the model, and said the tool
    could not compute it. The scorer reads rows only, as fixed before the run, so the score
    stands and was not changed. This reading is the controller's and is not a second score.
  - Total spend on the model API so far: 7.05 EUR of 20 (pilots 0.23, full run 6.82), priced
    from the token counts in the responses, not checked against the invoice.

- Third arm, text-to-SQL with the schema and no rules (`sql_schema_only`), run id
  `20261112T153515Z`, kept in `results/schema_only/`. The owner asked for it after the first run,
  whose result was known: the text-to-SQL arm had no wrong value, and `docs/schema.md` gives it
  rules R1 to R10. The first run was not rerun, rescored or edited; its file has the same hash
  before and after.
  - The arm's context is generated: how the tool runs, every table and column with its type,
    the meter tree. It has no rules, no column meanings, no metric definitions, no dataset range
    and no guidance on questions with no answer. Everything else is the `sql` arm's.
  - Preregistered before its first call: same 55 questions, 5 repeats, minimum 3, cap 8 EUR; a
    difference is claimed per group only when the 95% interval excludes 0 and the difference is
    at least 5 points. A reviewer checked the change before the run and found that a shorter
    later run would have cut the first run's numbers in the report; that was fixed before the
    results were read.
  - Pilot of three questions (H5, M1, U1): 0.08 EUR. Full arm: 275 runs, 5 of 5 repeats,
    6.78 EUR, one attempt on H1 timed out at 300 s and was retried (0.01 EUR unscored).
  - Outcomes: 263 of 275 as the key expects. 12 runs returned a wrong value without warning:
    4 double counts (H19 once, H20 three times of five), 3 on energy per unit (M2 twice, M3
    once), 5 values returned for a question with no answer (U5 four times, U2 once).
  - By the preregistered rule no group difference between `sql` and `sql_schema_only` is
    claimed: hierarchy differs by 4 points (interval 0 to 10), metric definition by 10 (0 to
    23). Both intervals reach 0. The counts are reported as counts.
  - The controller read one transcript per wrong question. H19 and H20 add a department to its
    own lines or machines. M2 and M3 count an order's units on the day the order starts or
    ends instead of spreading them over its intervals; that convention is rule R5, which this
    arm was never given. U5 returns the solar meter's kWh as consumption; U2 returns NULL for a
    month outside the data. This reading is the controller's and is not a second score.
  - Total spend on the model API: 13.93 EUR of 20, priced from the token counts in the
    responses, not checked against the invoice.

- Correction after a fact check of the README by a second agent, which read all 42 transcripts
  scored wrong. The entries above call those runs wrong "without warning" only in the scorer's
  sense: the scorer reads the returned rows and not the model's text. In the text, 9 of the 12
  `sql_schema_only` runs flagged the problem (all four double counts, the four solar runs and
  the month outside the data), 2 noted that their convention could shift the figures and 1 said
  nothing. Of the 30 `semantic` runs, all 30 gave the reference value in the text and 24 said
  the model had done the arithmetic itself. The report's headline label was changed from "wrong
  value without warning" to "returned rows held a wrong value (the model's text is not scored)".
  No score changed. H19 and H20 ask in so many words for the sum the key counts as wrong; the
  README says so. The same check found that the report fix was committed 73 seconds after the
  third arm's run started, so it was in place before the results were read, not before the run.

- Code review of the whole branch after the README, by a separate agent. Ten findings; five
  were fixed the same day.
  - The accuracy intervals used every run as an independent observation, although the five
    repeats of a question mostly agree. They now use the number of questions as the sample
    size and are wider (hierarchy for the `sql` arm: 96% to 100% became 85% to 100%). This
    differs from the preregistration, which named the Wilson interval without saying over
    what; the change goes in the cautious direction and no count or difference changed.
  - Percentages are now withheld for every arm when any arm is below the minimum repeats; a
    run of a single question is no longer marked failed for not reading the cache; the
    generator writes to the path every reader opens; the test workflow also runs the page's
    tests and build.
  - Left as they are and known: the demo's streaming path does not sum usage or check the
    served model (the recording command prices spend through its own callback); the SQL guard
    refuses a parenthesised join that DuckDB accepts, which happened in none of the 825
    recorded runs (one tool call failed in all of them, on an engine error); transcripts
    repeat the system prompt; usage is summed in three places; the saved sample answers for
    the page are not recorded.

## Owner rewording

none yet
