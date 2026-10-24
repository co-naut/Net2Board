# The typical move's per-probe breakdown — 2026-10-22 (#106)

The #106 dated receipt. The ADR-0016/0019 trigger fired on #112's readings (104.2 and 121.4 µs/probe on the 1–2-pad bucket), and the pre-registered consequence — a pad-level grid — met its profile first: across all four scopes (1–2 pads, 9+ pads, J4, U6) neighbour lookup is 5–12% of a probe, so the grid cannot clear the 100 µs bar. #106's amendment retargets the consequence: a breach graduates the measured top own-time term, this firing the net-span recompute (29% of a passive probe — ticket #113). The 100 µs bar, the #112 subject, and the report-only discipline are unchanged.

- **Command**: two detached in-process runs (long-runs recipe) over `run_tiny_session()`'s searched board: each scope re-solved at `BUDGET_TINY` twice, once profiler-off for the µs/probe wall (full solve minus the budget-zero fixed cost — the harness's own pairing) and once under cProfile for the shares. Run one profiled the 1–2-pad bucket; run two re-read it profiler-off twice, then profiled the 9+ bucket, J4, and U6. No perf JSON written; report-only (ADR-0026) — nothing asserts on any number here.
- **Tree**: `dev` @ `0eb85c8`.
- **Budgets**: `BUDGET_TINY` 2,500,000.
- **Provenance** (from the runs): Python 3.12.12 · Linux x86_64 · machine on mains, performance governor, load ~1.5–2.7 of 8 cores · no kicad-cli involvement (the tiny session has no oracle step).

## Per-probe cost by scope (shares of `_plan` cumulative)

cProfile's own wall inflates the absolutes, so the shares are the reading and the µs/probe row comes from the profiler-off solves.

| Row | 1–2 pads | 9+ pads | J4 (80 pads) | U6 (61 pads) |
|---|---|---|---|---|
| µs/probe, profiler off | **107.4 / 114.4** | 672.9 | 1,766.5 | 911.5 |
| probes | 121,590 | 26,440 | 3,147 | 3,153 |
| neighbour lookup (`_copper_query` + `_grid_query`) | 5.4% | 8.0% | 12.1% | below the print cut |
| net-span recompute (`_objective_changed`) | **28.9%** | 15.3% | 10.5% | 18.5% |
| pad finding records (pair + edge; wording rides inside) | 14.1% | 34.2% | 38.2% | 34.5% |
| pad copper shapes (`_pad_coppers`) | 7.4% | 13.2% | 14.0% | 17.8% |
| courtyard overlap (`_overlap_changed`) | 11.2% | 10.5% | 12.5% | 3.3% |

## Reading notes

- The net-span term is the passive hot spot: 238,379 `_net_span_term` calls over 121,590 passive probes (~2 per probe — the moved part's two nets), with ~21.2M list appends under it (~89 points per term — the net's pin count). The term is O(net degree) per probe, and tiny_tapeout's power nets make even a 2-pad passive pay the whole net. Filed as **#113** (`ready-for-agent`), the ticket this amendment graduates.
- Neighbour lookup is not the lever anywhere: 5.4% on the subject bucket, 12.1% on J4 — and the copper half a pad-level grid would replace is smaller still (passives 2.7%, J4 5.5%). The grid's recorded trigger (top cumulative row of any over-bar reading) does not fire today.
- Big parts build findings, not neighbours: 34–38% of their probes go to pad-pair and pad-edge finding records, ~10 per probe on the 9+ bucket and ~49 on J4 (its 80 edge-mounted pads price 153,970 edge records over 3,147 probes). The #64 counted-verdict pattern is the lead there; those rows are report-only and outside the bar, so this is a receipt note, not a ticket.
- The first passive read (226 µs, run one) was a launch-load outlier: the profiler-off re-reads in run two (107.4, 114.4) sit inside #112's same-day spread (104.2 / 121.4), at identical probe counts. Shares within one profile are unaffected.
- The retarget decision and the grid's new trigger are recorded in ADR-0016's amendment (this date); ADR-0019's thresholds line carries the pointer.
