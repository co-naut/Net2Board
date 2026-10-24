# The typical move reads the 1–2-pad bucket — 2026-10-20 (#112)

The #112 dated receipt. The ADR-0016/0019 trigger's subject is now the **1–2-pad bucket's µs/probe** — that bucket's scoped-solve probe wall over its own `candidates_evaluated` — derived from the move mix the harness already measures (`typical_move(mix)`; no solves of its own). The retired subject was a truncated-half slope of two full solves: the scan visits reach-descending, so its trailing half mostly priced late, low-reach passives — neither the mean nor a clean passive figure — and the subtraction of two near-equal walls amplified timing noise (identical trees read 110.75 and 151.3 µs the same day, #102's validation vs #106's triage; post-#111 the slope read 138.5 µs while the passive bucket read 110). A bucket's reading is order-free by construction. Every bucket and named row now carries the probe count it derives over, so per-probe is derivable from the perf JSON alone — the hand count pass the receipts needed since #106 is gone.

- **Command**: bench first `uv run python -m net2board.examples.profile tiny --bench 20000` (210 µs/probe, in the receipt range 206–219), then two back-to-back detached `session --slow` runs (long-runs recipe; run1 633 s, run2 ~700 s, both `exit=0`, launch load ~1.4 of 8 cores).
- **Tree**: Net2Board-dev `dev` @ this change (uncommitted at launch); prior readings from #111's receipt @ `c23578e`.
- **Budgets**: `BUDGET_UNDERCUT` 1,600 · `BUDGET_MAIN` 20,000 · `BUDGET_TINY` 2,500,000

## The subject, twice (ADR-0026's ±20% band)

| Run | subject (1–2 pads µs/probe) | probes | spread vs run1 |
|---|---|---|---|
| run1 | **104.2** | 121,590 | — |
| run2 | **121.4** | 121,590 | +16.5% — within band |

Probe scale identical across runs (121,590; bucket sum 164,222 == `TINY_HEADLINE_CANDIDATES` both runs — the slow-tier pin moved from the slope's scale to this sum and held). For contrast, the retired slope's same-day spread on identical trees was 110.75 → 151.3 (+37%).

Run1's other readings, for the next run to compare against: session wall 247.61 s · 1,249,767 candidates (5,047/s) · stop `no_further_improvement` · replay byte-equal `True` · ratio 15,458×. Run2: ratio 17,961×. Run2's perf JSON is the file on record (the day's last run wins, ADR-0026/#100).

## Reading notes

- Both runs printed `no perf history to compare`: the only perf JSON on record is today's, and a run is never its own history (#100). Same-day comparison of the two runs is this receipt's table.
- Every reading drifted up together in run2 (1–2 pads +16.5%, 3–8 +19.4%, 9+ +21.7%, J4 +21%, U6 +12.3% at identical probe counts) — a window-wide drift, not a subject-specific instability; the subject's band judgment rides the drift it shares with its report-only company.
- The subject still reads over the 100 µs line (104.2 / 121.4), as it did in #111's receipt (110 µs). The pad-level-grid graduation call stays with #106, per #112's out-of-scope boundary.
- Run1's bucket walls (12.67 / 3.12 / 15.74 s) and named rows (J4 5.35 s, U6 3.11 s) are in the run log; run2's are the ones the perf JSON carries.
