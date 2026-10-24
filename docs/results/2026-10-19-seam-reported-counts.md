# Seam-reported candidate counts — 2026-10-19 (#102, with #107)

The #102 dated receipt. The slow tier's candidate counts are read off the session's own results now: `SolveResult` carries `candidates_evaluated` (`min(budget, N)`, the honest ledger the Budget spends), `SessionStep` records it, and the harness's three derivation sites — the entry pin's flip-point pair, `typical_move_us`'s derived scale, and `session_candidate_counts`' per-step gallop+bisection — are gone. `candidate_count` is deleted with its `expect` fast path (#94). The counter landed on top of #107's stop-reason honesty fix (a budget that truncates a scan stops `BUDGET_EXHAUSTED`, never a rounded-off NFI), which is what makes the count and the old derivation comparable at all: with truncation-mid-scan reporting honestly, the minimal-NFI budget *is* the untruncated count, and the counter reads it exactly.

- **Command**: `uv run python -m net2board.examples.profile tiny --bench 20000` (bench first), then `uv run pytest -m slow -q`
- **Tree**: Net2Board-dev `dev` @ `41b88d0` (#107 `0e66e6b` + #102 `41b88d0`, pins deliberately stale at #105's values — the one expected failure below is the re-pin mechanism working).
- **Provenance** (from the run): Python 3.12.12 · kicad-cli 10.0.7 · Linux x86_64 · bench 219 µs/probe on the tiny entry board (the same rate #105's run read, so the wall comparison is clean).
- **Budgets**: `BUDGET_UNDERCUT` 1,600 · `BUDGET_MAIN` 20,000 · `BUDGET_TINY` 2,500,000

## The run (verbatim, the failure that is the point)

```text
..........................F.......                                       [100%]
E       AssertionError: the entry board's count drifted from
E       TINY_ENTRY_CANDIDATES — re-pin it to 1,249,767
E       assert 1249767 == 1249736
E        +  where 1249767 = MacroRow(... candidates_evaluated=1249767,
          ... typical_move_us=151.2863750776229,
          typical_move_scale=164222).candidates_evaluated
1 failed, 33 passed, 934 deselected, 5 warnings in 1407.65s (0:23:27)

994.31s setup    tests/test_session_tinytapeout.py::TestTinyAcceptance::...
398.30s setup    tests/test_oracle_tinytapeout.py::TestSearchedTinyBoardAgainstTheOracle::...
```

## What it bought

| | #105's run (pre-#102) | this run | Δ |
|---|---|---|---|
| `pytest -m slow` wall | 40:17 | **23:27** | −16.8 min |
| `tiny_acceptance` fixture (`--durations`) | 2063.8 s | 994.3 s | −17.4 min |
| oracle searched-board fixture | 338.4 s | 398.3 s | +60 s (noise; both ~5.6–6.6 min) |
| headline session itself (`MacroRow.wall_s`) | 348 s | 360 s | noise |

The saving outruns the ~11–12 min estimate: the entry flip check (~10 min) and the typical-move derivation (~2 min) died as predicted, and `session_candidate_counts`' per-step gallop+bisection — "the rest of the 34.4 min" the triage priced loosely — turned out to carry the remaining minutes. Nothing was optimized: every removed probe was a re-walk of a solve the session had already done.

## The +31, twice

Both re-pins moved by exactly 31 — entry 1,249,736 → **1,249,767**, headline 164,191 → **164,222** — the same gap the triage measured on the fast fixtures (`placed_r1` 397 vs 366, `converged` 112 vs 81): the old derivation stopped at the minimal budget whose truncated final round still reported NFI, low by the last mover's unfinished scan. #107 forbids that label on an unfinished scan, the counter counts the whole walk, and the two definitions coincide — at tiny_tapeout scale as at toy scale. The headline's `typical_move_us` reads 151.3 µs/probe on the searched board (report-only, ADR-0026), against the entry board's 219.

Fast tier: 928 passed, 6 skipped at `41b88d0` (97.3 s wall). The ecc83 golden gained only the `candidates_evaluated` field (undercut 1600/1600 exactly — #107's exhausted-spend contract, golden-pinned); every other reading byte-identical. Fast-tier wall honesty: the tier was already over its 60 s ceiling before this session (72.4 s at `56a7cd5`, measured in a clean worktree); #102's and #107's exhaustive budget-sweep tests add ~25 s of it. The sweeps are the regression lock — the point is that *every* truncated budget reads the honest label — so the overrun is filed, not trimmed (the ceiling ticket).

## Re-pin

Landed with this receipt: `TINY_ENTRY_CANDIDATES = 1_249_767`, `TINY_HEADLINE_CANDIDATES = 164_222` — the counter's exact counts, each +31 over the retired derivation's reading, the quiet tail made visible.
