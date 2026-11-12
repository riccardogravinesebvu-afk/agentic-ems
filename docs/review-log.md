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

## Owner rewording

none yet
