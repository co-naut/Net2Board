# M2 results — 2026-10-05 (#63 live-placements fix re-run)

The ADR-0019 dated receipt after #63's fix: `_commit` maintains the
live placements map, so the wirelength candidate families see netmates
placed during the call. Every number below comes from one run of the
committed harness on one machine — the ratio's two terms were measured
together, so the ratio is self-normalizing. The stale-map receipts this
one supersedes: [2026-10-01-m2-order-amendment.md](2026-10-01-m2-order-amendment.md)
and [2026-10-01-m2.md](2026-10-01-m2.md).

- **Command**: `uv run python -m net2board.examples.session --slow`
- **Tree**: Net2Board-dev `dev`, #63's commit (live placements map;
  `BUDGET_TINY` re-pinned 500,000 → 2,500,000)
- **Provenance** (from the run): Python 3.12.12 · kicad-cli 10.0.6 ·
  freerouting-2.4.1, jar sha256
  `251101c3eeac22d7e7dfcf6796603279e5d1000283eb82d8f093780f7afc6aa9` ·
  Linux 7.2.8-zen1-2-zen x86_64 (glibc 2.44)
- **Budgets**: `BUDGET_UNDERCUT` 1,600 · `BUDGET_MAIN` 20,000 ·
  `BUDGET_TINY` 2,500,000

## Harness output (verbatim)

```text
provenance:
  python: 3.12.12
  kicad_cli: 10.0.6
  jar_sha256: 251101c3eeac22d7e7dfcf6796603279e5d1000283eb82d8f093780f7afc6aa9
  os: Linux-7.2.8-zen1-2-zen-x86_64-with-glibc2.44
  cpu: x86_64
  fixtures: {'ecc83': '15 comps / 9 nets / 40x30 mm', 'tiny_tapeout': '150 comps / 114 nets / 104.5x81 mm / 4 Cu'}
  budgets: {'BUDGET_UNDERCUT': 1600, 'BUDGET_MAIN': 20000, 'BUDGET_TINY': 2500000}
first act, wrapper baseline on tiny_tapeout: 30 candidates, 3.554228 s/candidate, slowest/fastest 1.370 (linearity receipt)
tiny_tapeout, per-session (macro): wall 229.51 s, 1,414,784 candidates (6164/s acceptance-path), stop no_further_improvement, unmet [], stranded []
  objective: 0 nm before -> 5,958,231,697 nm after
  derived baseline: 3.554228 s/candidate x 1,414,784 candidates = 5,028,464.3 s
  ratio: 21,910x (floor 1,000x)
  replay byte-equal: True
  move mix (improve-round wall): 1-2 pads 63% · 3-8 pads 10% · 9+ pads 26%; typical move 137.7 us/probe
ecc83, per-candidate (micro): engine 101.5 us, wrapper 3.254 s (20 candidates, slowest/fastest 1.233), ratio 32,062x
adr-0001 re-measures @150 placements: board 91.4 KiB, retained snapshot 0.21 KiB marginal, with_placement 7.6 us
```

## The linearity receipt

Per-candidate wrapper cost is what licenses the derived baseline's
multiplication (baseline = per-candidate × the session's candidate
count, arithmetic printed above):

- tiny_tapeout: 30 real export + `kicad-cli pcb drc` candidates on the
  searched 150-footprint board — mean **3.554228 s**, slowest/fastest
  **1.370**
- ecc83: 20 candidates on the searched board — mean **3.254 s**,
  slowest/fastest **1.233**

## Readings

- **tiny_tapeout, per-session (macro)**: full-budget solve from the
  all-unplaced board — **1,414,784 candidates** (derived through the
  seam, not instrumented), 229.51 s wall, `NO_FURTHER_IMPROVEMENT`,
  replay byte-equal, **nothing stranded**, objective
  **5,958,231,697 nm** — **−24.0%** against the stale-map headline
  (7,839,925,000 nm). Derived wrapper baseline **5,028,464 s (~58 d)**
  vs 229.51 s: **21,910×** (floor 1,000×).
- **The bug's signatures are gone.** The stale-map search's
  grid-first-fit signature — placements at x ≡ 1 mm (mod 2 mm) on both
  axes, measured at 93/150 in #63 — reads **6/150** on the searched
  export (lattice coincidences, not the fallback scan: the families now
  place pad-aligned). And the searched board is a **fixed point** of
  `solve` again: the stale-map board re-solved for −1,653,189,870 nm
  more (#63's evidence); this one re-solves to identity, the replay
  gate asserts it byte for byte.
- **`BUDGET_TINY` re-pin**: the old 500,000 was sized (#49's rule) to
  the crippled search's convergence point (284,866) and the fixed
  search exhausts it — the harness's own guard fired
  ("a BUDGET_EXHAUSTED headline means the pin is wrong"). Sizing
  probes through the public seam: 1,000,000 exhausts (objective
  5,962,039,732 nm), 2,000,000 converges (5,958,231,697 nm — the
  receipt's own headline reading), bracketing the convergence in
  (1,000,000; 1,500,000]; the pin is the round 2,500,000, and the
  run's derived count puts the headroom at **1.77×** (the stale pin
  sat at 1.755× — same rule, new search).
- **Comparability discipline**: the new tiny objective
  (5,958,231,697 nm) and the stale-map runs' (7,839,925,000 /
  7,709,035,000) are **incomparable** — different searches, different
  placed sets (#31's incomparable-totals evidence). The candidate
  count differs for the same reason (1,414,784 vs 284,866): the
  families the stale map blinded are now probed, which is the fix.
- **ecc83 unchanged, byte for byte**: the session's every step enters
  pre-placed (the committed hand table) and converged, so the live
  placements map is behavior-neutral on that path — the session
  golden and export golden needed no re-pin, and the micro ratio
  moved only on machine walls (101.5 µs vs 83.9 µs engine,
  32,062× vs 30,413×). Matches #63's account of why every ecc83
  golden passed while the hard fixture degraded silently.
- **The strand reading**: none — J9 places with the field, and the
  families' anchors now include every mid-call placement.
- **#51's trigger re-measure** (the ruling's sequencing step): the
  fixed search's typical move reads **137.7 µs/probe** — still above
  ADR-0016's 100 µs line, so the trigger fires on the fixed search's
  own mix (1–2 pads 63% · 3–8 pads 10% · 9+ pads 26%). The
  re-measure and the graduation question live on #51.

The slow-tier suite (`uv run pytest -m slow`) is green on this tree:
the derived ratio clears the floor, the linearity receipt is recorded,
the headline replays byte for byte, and the pinned readings assert.

Reproduce with `uv run python -m net2board.examples.session --slow` —
the candidate count and objective are deterministic; the wall times and
ratios are machine-facts of the run that produced them.
