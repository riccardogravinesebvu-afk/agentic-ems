# Error classes and what version 1 does with each

These are errors typical of energy analytics systems in general. Each one is either enforced
by the semantic layer, tested by a question in `src/agentic_ems/benchmark/questions.toml`, or
left out with its reason. The question ids in the table are those of the draft question list;
the mapping to the final ids follows the table.

| Class | Typical error | In version 1 |
|---|---|---|
| P1 | A meter summed with meters downstream of it | Layer rule and the whole hierarchy group |
| P2 | An allocation share applied to a physical reading | Out: the synthetic plant has no cost allocation |
| P3 | Different quantities (energy, water, gas) in one sum | Partly: only electricity exists; U3 and H21 |
| P4 | Consumption confused with generation or net balance | H21, O1, O3 |
| P5 | A generation-only meter queried as consumption | Layer rule (rejected with a message, never filtered silently) and U5 |
| P6 | Daily statistics computed on finer rows | O13 |
| P7 | Power profile not summed per instant | Layer rule for peak and H22 |
| P8 | The amount above a threshold reported as the peak | Out: no threshold records in the data; O11 is the nearest question |
| P9 | Interval energy reported as power, or the reverse | Layer rule (interval kWh times 4) and C2 |
| P10 | A time window that does not meet the data | Layer rule (period outside the dataset is an error stating the range), U2, U6 |
| P11 | A partly covered period compared with a full one | U6; comparisons themselves are out of scope (O5) |
| P12 | Ambiguous week boundaries | O14 |
| P13 | UTC instants grouped without conversion to local time | Layer rule (local-time columns), C10, C11, H16 |
| P14 | A correct number described with the wrong grain | Demo: the headline is rendered by code from the rows; prose is never scored |
| P15 | A total rebuilt from a truncated result | Layer rule: the row limit is an error, never a truncation |
| P16 | Cost, emissions or thresholds from coefficients that are not in the data | Layer rule (cost uses the tariff table), U4, U7 |
| P17 | An unrecognised name reported as zero consumption | Layer rule (unknown meter is an error), U1, C13 |
| P18 | Consumption lost through an incomplete link to a department | Out: the synthetic tree has one parent link and it is complete |
| P19 | Scope narrowed or widened by mistake | U8 |
| P20 | Counter resets, gaps and scale factors ignored | Out: the generator writes clean interval energy; stated as a limit in `docs/golden.md` |

## Draft ids and final ids

| Draft | Final |
|---|---|
| H1 to H20 | H1 to H20 |
| H21 | rejected (`[[rejected]]` in the questions file) |
| H22 | H21 |
| C1, C2, C3 | C1, C2, C3 |
| C7, C8, C10, C13 | C4, C5, C6, C7 |
| C4, C5, C6, C9, C11, C12 | M1, M2, M3, M4, M5, M6 |
| O1 to O14 | O1 to O14 |
| U1 to U8 | U1 to U8 |
