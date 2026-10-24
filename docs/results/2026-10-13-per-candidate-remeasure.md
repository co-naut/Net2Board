# M3 results — 2026-10-13 (#94, the slow face was never fast)

The #94 dated receipt. The suspicion the ticket opened under — the Oct-6 cluster (#65/#66/#67/#69) costing the slow tier 2–10× against the 2026-10-05 receipt's ~10–12 min estimate — dissolves on measurement: the estimate priced the face's six *visible* solves and missed what `candidate_count`, the derivation behind every count, actually spends. There is no good state to regress from: the face was born 2026-10-01 (#49, `BUDGET_TINY` 500,000), re-priced 2026-10-05 (#63's re-pin to 2,500,000, ~5× the candidates), and the 2026-10-05 receipts timed `examples.session --slow`'s headline solve — 97.66 s, one walk — never the pytest face, whose only recorded reading there is "green", with no wall. The fix is harness-side and lands with this receipt: `candidate_count` gains the `expect` fast path (the flip-point pair — two solves), the two full-scope derivations get pinned readings, and the slow tier asserts the pins. The solver, the objective, and the searched result are untouched: the headline replays byte-equal at the same candidate count and objective. #82's wrong-suspicion comment and this ticket's suspect list shared the premise this receipt corrects — no commit made the engine slower; the face's price was miscounted from birth. (The pad-check retraction in #94's body stands as written.)

- **Command**: `uv run python -m net2board.examples.session --slow`, then `uv run pytest -m slow --durations=0`
- **Tree**: Net2Board-dev `dev` @ `5b87b99` + #94's fix (the `expect` fast path, the pins, the scale in the report; uncommitted at run time). The before side is the same commit without the fix, in a clean worktree.
- **Provenance** (from the run): Python 3.12.12 · kicad-cli 10.0.7 · freerouting-2.4.1, jar sha256 `251101c3eeac22d7e7dfcf6796603279e5d1000283eb82d8f093780f7afc6aa9` · Linux 7.2.9-zen1-1-zen x86_64 (glibc 2.44) — the before side read kicad-cli 10.0.6 on Linux 7.2.8-zen1: an upgrade between the measurements, not a treatment effect (see Version drift).
- **Budgets**: `BUDGET_UNDERCUT` 1,600 · `BUDGET_MAIN` 20,000 · `BUDGET_TINY` 2,500,000

## Harness output (verbatim)

```text
provenance:
  python: 3.12.12
  kicad_cli: 10.0.7
  jar_sha256: 251101c3eeac22d7e7dfcf6796603279e5d1000283eb82d8f093780f7afc6aa9
  os: Linux-7.2.9-zen1-1-zen-x86_64-with-glibc2.44
  cpu: x86_64
  fixtures: {'ecc83': '15 comps / 9 nets / 40x30 mm', 'tiny_tapeout': '150 comps / 114 nets / 104.5x81 mm / 4 Cu'}
  budgets: {'BUDGET_UNDERCUT': 1600, 'BUDGET_MAIN': 20000, 'BUDGET_TINY': 2500000}
first act, wrapper baseline on tiny_tapeout: 30 candidates, 2.950264 s/candidate, slowest/fastest 1.196 (linearity receipt)
tiny_tapeout, per-session (macro): wall 99.91 s, 1,414,784 candidates (14160/s acceptance-path), stop no_further_improvement, unmet [], stranded []
  objective: 0 nm before -> 5,958,231,697 nm after
  derived baseline: 2.950264 s/candidate x 1,414,784 candidates = 4,173,986.1 s
  ratio: 41,776x (floor 1,000x)
  replay byte-equal: True
  move mix (improve-round wall): 1-2 pads 54% · 3-8 pads 10% · 9+ pads 36%; typical move 53.8 us/probe (scale 164,019)
ecc83, per-candidate (micro): engine 63.5 us, wrapper 2.790 s (20 candidates, slowest/fastest 1.097), ratio 43,965x
adr-0001 re-measures @150 placements: board 91.4 KiB, retained snapshot 0.21 KiB marginal, with_placement 6.5 us
```

## The before side: what 49:31 was

The durations run at `5b87b99` (pre-fix): 29 passed in 2971.55 s (49:31), of which **one setup — `measure_acceptance()` — is 2825.13 s**; every other duration is under 5 s except the oracle test's own tiny session (133.90 s). The face is one harness call, and its inside, measured in pieces with throwaway solve-call counters:

- **The derivation's shape.** On the conftest 3-comp board (N = 386, ceiling 1,000,000), one `candidate_count` is **19 solves** — the ceiling probe, the 1→512 gallop, eight bisection probes: ~2·log₂(N) + O(1) solves, each paying `min(budget, N)` real candidates.
- **The entry-board derivation** (N = 1,414,784, ceiling 2,500,000): the same shape at macro scale — ~30 probes ≈ 19×N ≈ 27M candidate-units, ~1,600 s at the run's own rates (accounted by subtraction from the 2825 s, not separately timed).
- **The headline-board derivation, timed whole: N = 164,019, 38 solves, 291.7 s.** The searched board is the objective's fixed point but not a one-probe fixed point — a fresh solve from it walks 164,019 probes to re-converge. This hides inside `typical_move_us`, which derives its own scale.
- **The rest**: headline + replay sessions (~88 s each in the derivation run), the wrapper baseline (~80 s), typical-move's two timed solves, `move_mix`'s three, the ecc83 micro row, ADR-0001's re-measures. Sum ≈ the 2825 s.

The ticket's "six solve-scale walks ≈ 10–12 min all-in" counted only the visible solves; the two derivations were worth ~34 min of solve-equivalents on the same run. And the implied ~59 µs per candidate-unit on the entry derivation sits inside the 2026-10-05 receipt's own 44.4–69 µs/probe band — the engine's per-probe cost did not move; what moved was the walk count (#63's ~5× candidates did that on 10-05, and the multiplier was always there).

## The fix: `expect`, the flip-point fast path

`candidate_count(board, refs, *, ceiling, expect=None)`: a correct `expect` is proven by the flip-point pair — `solve(expect)` stops `NO_FURTHER_IMPROVEMENT` **and** `solve(expect - 1)` reads `BUDGET_EXHAUSTED` (monotonicity: ADR-0013's budgets change where the search stops, never its path — the same assumption the gallop+ bisection already runs on) — so the ~2·log₂(N)-solve walk collapses to **two solves**. A miss falls back to the full search and the derived truth still flows into the report; the slow tier's new pin assertion (`test_the_derived_counts_hold_their_pins`) fails naming the fresh N — a re-pin, like a golden. Still derived, not instrumented: the check runs budgeted solves through the public seam; the Evaluator is never touched (the counting trick remains the tests').

Pins: `TINY_ENTRY_CANDIDATES` = 1,414,784 · `TINY_HEADLINE_CANDIDATES` = 164,019, both derived by the full search on 2026-10-13. The complexity assertion (ADR-0016 fast-tier style): `test_a_known_count_reads_in_two_solves` holds `budgets == [known, known - 1]` — exactly two solves, a claim about solves, not minutes.

## After

- `pytest -m slow --durations=0`: **30 passed in 698.02 s (11:38)**; the `measure_acceptance` setup **589.68 s** — 4.8× off the 2825.13 s before. Both pins held.
- Macro row: 99.91 s for 1,414,784 candidates (14,160/s acceptance-path), objective 0 → 5,958,231,697 nm, replay byte-equal, nothing stranded — the fix is behavior-neutral on the search.
- Typical move **53.8 µs/probe** (the report now prints its scale): under ADR-0016's 100 µs line on this machine — #92's trigger reading should take this receipt, not the 44.4 µs/probe row it was waiting to supersede.

## Version drift between the sides

The before side ran kicad-cli 10.0.6 on Linux 7.2.8-zen1; the after side 10.0.7 on 7.2.9-zen1 — a machine-side upgrade between the measurements. Candidate counts and the objective are deterministic and identical across both sides (1,414,784 · 5,958,231,697 nm), which is the comparability that matters; walls and per-probe microseconds are machine-facts of the runs that produced them, per the 2026-10-05 receipts' own discipline.

## Readings

- **Macro row**: wall 99.91 s, 1,414,784 candidates, derived baseline 2.950264 s/candidate × 1,414,784 = 4,173,986 s, ratio **41,776×** (floor 1,000×) — supersedes the [2026-10-05-m2-index-amendment.md](2026-10-05-m2-index-amendment.md) row (97.66 s · 44.4 µs/probe · 39,597×), which the README's Measured table now points past.
- **Micro row**: engine 63.5 µs, wrapper 2.790 s/candidate, ratio **43,965×**.
- **#85's sign-off re-measure** reads the README rows — they now name this receipt, so a sign-off before this landed would have published numbers it would have to supersede.

The slow-tier suite (`uv run pytest -m slow`) is green on this tree: 30 passed — the derived ratio clears the floor, the linearity receipt is recorded, the headline replays byte for byte, the pinned readings assert, and the derivation pins hold.

Reproduce with `uv run python -m net2board.examples.session --slow` and `uv run pytest -m slow` — the candidate counts and objective are deterministic; the wall times and ratios are machine-facts of the run that produced them.
