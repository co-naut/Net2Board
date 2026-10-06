# M2 results — 2026-10-01 (#52 order amendment re-run)

The ADR-0019 dated receipt after #52's board-derived reach-descending
visit order: every number below comes from one run of the committed
harness on one machine — the ratio's two terms were measured together,
so the ratio is self-normalizing. The lexicographic-order receipt this
one amends is [2026-10-01-m2.md](2026-10-01-m2.md); the strand ruling
and the falsification protocol are on #52.

- **Command**: `uv run python -m net2board.examples.session --slow`
- **Tree**: Net2Board-dev `dev`, #52's commit (reach-descending visit
  order)
- **Provenance** (from the run): Python 3.12.12 · kicad-cli 10.0.6 ·
  freerouting-2.4.1, jar sha256
  `251101c3eeac22d7e7dfcf6796603279e5d1000283eb82d8f093780f7afc6aa9` ·
  Linux 7.2.7-zen1 x86_64 (glibc 2.44)
- **Budgets**: `BUDGET_UNDERCUT` 1,600 · `BUDGET_MAIN` 20,000 ·
  `BUDGET_TINY` 500,000

## Harness output (verbatim)

```text
provenance:
  python: 3.12.12
  kicad_cli: 10.0.6
  jar_sha256: 251101c3eeac22d7e7dfcf6796603279e5d1000283eb82d8f093780f7afc6aa9
  os: Linux-7.2.7-zen1-1-zen-x86_64-with-glibc2.44
  cpu: x86_64
  fixtures: {'ecc83': '15 comps / 9 nets / 40x30 mm', 'tiny_tapeout': '150 comps / 114 nets / 104.5x81 mm / 4 Cu'}
  budgets: {'BUDGET_UNDERCUT': 1600, 'BUDGET_MAIN': 20000, 'BUDGET_TINY': 500000}
first act, wrapper baseline on tiny_tapeout: 30 candidates, 2.709966 s/candidate, slowest/fastest 1.179 (linearity receipt)
tiny_tapeout, per-session (macro): wall 27.12 s, 284,866 candidates (10504/s acceptance-path), stop no_further_improvement, unmet [], stranded []
  objective: 0 nm before -> 7,839,925,000 nm after
  derived baseline: 2.709966 s/candidate x 284,866 candidates = 771,977.1 s
  ratio: 28,465x (floor 1,000x)
  replay byte-equal: True
ecc83, per-candidate (micro): engine 83.9 us, wrapper 2.552 s (20 candidates, slowest/fastest 1.115), ratio 30,413x
adr-0001 re-measures @150 placements: board 91.4 KiB, retained snapshot 0.21 KiB marginal, with_placement 5.8 us
```

## The linearity receipt

Per-candidate wrapper cost is what licenses the derived baseline's
multiplication (baseline = per-candidate × the session's candidate
count, arithmetic printed above):

- tiny_tapeout: 30 real export + `kicad-cli pcb drc` candidates on the
  searched 150-footprint board — mean **2.709966 s**, slowest/fastest
  **1.179**
- ecc83: 20 candidates on the searched board — mean **2.552 s**,
  slowest/fastest **1.115**

## Readings

- **tiny_tapeout, per-session (macro)**: full-budget solve from the
  all-unplaced board — 284,866 candidates (derived through the seam,
  not instrumented), 27.12 s wall, `NO_FURTHER_IMPROVEMENT`, replay
  byte-equal, legalize delta 0, `run_drc` clean, **nothing stranded**.
  Derived wrapper baseline **771,977 s (~8.6 h)** vs 27.12 s:
  **28,465×** (floor 1,000×).
- **The falsification protocol's four gates hold** (#52): 150/150
  placed with `TINY_STRANDED = ()`; the ecc83 record regressed on no
  step — `undercut` landed 1 mm *better* (321,050,000 vs 322,050,000
  nm at the same 1,600-candidate cut, the golden re-pinned
  deliberately), every later step's total identical, and the searched
  export bytes did not move at all; determinism and replay stayed
  green unchanged.
- **Comparability discipline**: the new tiny objective
  (7,839,925,000 nm) and the lexicographic run's (7,709,035,000) are
  **incomparable** — different placed sets (J9 in vs out, #31's
  incomparable-totals evidence). The candidate count also differs
  (284,866 vs 181,638): a different search path to its own local
  optimum, still ~1.75× inside `BUDGET_TINY`.
- **The strand reading**: none. J9 — the 62 mm pin header — places
  with the field under reach-descending order: attempted first, its
  candidate family is probed while the board is still empty.
- **Oracle note** (slow-tier suite, #49's face): the searched board
  carries one same-net pad contact — MUX_SEL's NT1 net-tie pad against
  U4's pin-1 TSSOP pad, centers 0.264 mm apart — so the oracle's
  ratsnest names one fewer net than the placed-multi-pad reference
  set. Contact is a pad fact outside M2's model (ADR-0006); the
  oracle test now asserts the subset direction, with the evidence in
  its docstring. Every gated per-type oracle check stayed clean.

Reproduce with `uv run python -m net2board.examples.session --slow` —
the candidate count and objective are deterministic; the wall times and
ratios are machine-facts of the run that produced them.
