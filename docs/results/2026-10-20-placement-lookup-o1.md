# Pad-finding placement lookup is O(1) — 2026-10-20 (#111)

The #111 dated receipt. Every pad finding a probe built rebuilt the ref → placement map over all placements to word itself — `_placement_of` called `_placement_index`, which on the Evaluator path iterated the `_MoveView` through a `ValuesView`: O(n) per finding, cProfile 39% of a passive probe's wall and 55% of a 9+-pad probe's (#106's triage). The builders now resolve through `board.placement(ref)` — Board's O(1) index on the full recompute (ADR-0001 as amended by #29), and a new `_ProbeBoard.placement` answering from the `_MoveView` on the probe path (c23578e). `_constraint_wording` had the same per-finding rebuild; it now reads `board.placement` per named ref with `board.component(name)` as the unplaced fallback — a maintainer-approved scope extension, recorded in the commit. Pinned in the fast tier as a count, not a wall (ADR-0016/0026): `test_pad_wording_does_not_walk_the_placement_view` spies on `_MoveView.__iter__` and asserts a probe's wording walks zero view items at n=40 and n=80 (pre-fix: 240 — 2n for one SHORTING pair at each size; independently confirmed to fail on pre-fix HEAD).

- **Command**: bench first `uv run python -m net2board.examples.profile tiny --bench 20000`, then detached `uv run python -m net2board.examples.session --slow` (long-runs recipe; 732 s wall, exit=0, machine quiet: load ~1.1 of 8 cores at launch). Bucket and named-mover walls come from the session's perf JSON; their probe counts come from a count pass afterwards — `run_tiny_session()`'s searched board, each scope re-solved at `BUDGET_TINY`, `candidates_evaluated` only. The counts reproduced #106's exactly (121,590 / 16,192 / 26,440, sum 164,222; J4 3,147; U6 3,153), so the walls pair cleanly across trees.
- **Tree**: Net2Board-dev `dev` @ `c23578e` (#111's fix); pre-#111 readings from #106's triage @ `e51e4f1`, same day, same machine.
- **Provenance** (from the run): Python 3.12.12 · kicad-cli 10.0.7 · Linux x86_64 · bench 206 µs/probe on the tiny entry board (the pre-#111 receipt read 219 µs; the bench board is passive-dominated and cross-run spread is loose — the buckets below are the reading).
- **Budgets**: `BUDGET_UNDERCUT` 1,600 · `BUDGET_MAIN` 20,000 · `BUDGET_TINY` 2,500,000

## Per-probe cost by move-mix bucket (searched tiny board, scoped solves)

| Reading | probes | pre-#111 (`e51e4f1`) | post-#111 (`c23578e`) | Δ |
|---|---|---|---|---|
| 1–2 pads (118 movers) | 121,590 | 156 µs | **110 µs** | −30% |
| 3–8 pads (19 movers) | 16,192 | 356 µs | **205 µs** | −42% |
| 9+ pads (13 movers) | 26,440 | 1,025 µs | **616 µs** | −40% |
| J4 (80 pads) | 3,147 | 2,596 µs | **1,861 µs** | −28% |
| U6 (61 pads) | 3,153 | 1,683 µs | **1,056 µs** | −37% |
| `typical_move_us` (scale 164,222) | 164,222 | 110.75 µs | **138.5 µs** | within spread |

This run's bucket wall shares: 1–2 pads 41% · 3–8 pads 10% · 9+ pads 49%. Headline macro row, for the next run to compare against: session wall 220.93 s · 1,249,767 candidates (5,657/s) · stop `no_further_improvement` · replay byte-equal `True` · ratio 20,652× (floor 1,000×).

## Reading notes

- The five targeted readings all dropped 28–42%, consistent with the cProfile shares: the wording term was 39–55% *cumulative*, and its children (capsule geometry, `_pad_at`) remain — removing the parent recovers most, not all, of its share.
- `typical_move_us` moved 110.75 → 138.5 µs at an identical scale — inside the cross-run spread already observed on identical trees (#102 read 151.3 on the same day #106 read 110.75, both pre-fix). The reading is order-dependent (#112) and single-sample; the buckets' consistent −28..−42% against identical probe counts is the signal.
- Passives (1–2 pads) sit at 110 µs/probe — still above the 100 µs line #106 flagged, and `typical_move_us` reads above it too. Per #106's pre-registered next step, the pad-level-grid graduation call is back to its maintainer with these numbers.
- Out of scope here, next own-time row under cProfile: `_net_span_term` (~43 µs/probe on passives, #106's secondary lead).
