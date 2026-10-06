# M2 results — 2026-10-05 (#64 ADR-0016 amendment re-run)

The ADR-0016 dated receipt after #64's amendment: the courtyard grid
graduates into the Evaluator's private machinery and the probe contract
gains a counted verdict — the search reads each probe's
`(error_count, total_nm)` without materializing the sorted findings tuple
or the term store. Every number below comes from one run of the committed
harness on one machine — the ratio's two terms were measured together, so
the ratio is self-normalizing. The receipt this one supersedes:
[2026-10-05-m2-placements-fix.md](2026-10-05-m2-placements-fix.md)
(and through it the stale-map runs).

- **Command**: `uv run python -m net2board.examples.session --slow`
- **Tree**: Net2Board-dev `dev`, #64's amendment (courtyard grid + counted
  probe verdict; `BUDGET_TINY` unchanged at 2,500,000)
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
first act, wrapper baseline on tiny_tapeout: 30 candidates, 2.733168 s/candidate, slowest/fastest 1.311 (linearity receipt)
tiny_tapeout, per-session (macro): wall 97.66 s, 1,414,784 candidates (14487/s acceptance-path), stop no_further_improvement, unmet [], stranded []
  objective: 0 nm before -> 5,958,231,697 nm after
  derived baseline: 2.733168 s/candidate x 1,414,784 candidates = 3,866,842.8 s
  ratio: 39,597x (floor 1,000x)
  replay byte-equal: True
  move mix (improve-round wall): 1-2 pads 54% · 3-8 pads 10% · 9+ pads 36%; typical move 44.4 us/probe
ecc83, per-candidate (micro): engine 56.9 us, wrapper 2.539 s (20 candidates, slowest/fastest 1.099), ratio 44,596x
adr-0001 re-measures @150 placements: board 91.4 KiB, retained snapshot 0.21 KiB marginal, with_placement 6.6 us
```

## The linearity receipt

Per-candidate wrapper cost is what licenses the derived baseline's
multiplication (baseline = per-candidate × the session's candidate
count, arithmetic printed above):

- tiny_tapeout: 30 real export + `kicad-cli pcb drc` candidates on the
  searched 150-footprint board — mean **2.733168 s**, slowest/fastest
  **1.311**
- ecc83: 20 candidates on the searched board — mean **2.539 s**,
  slowest/fastest **1.099**

## Readings

- **The determinism receipt held exactly** (#64's gate): the candidate
  count (**1,414,784**), the objective (**5,958,231,697 nm**), the stop
  reason, the empty unmet/stranded readings, and the byte-equal replay
  are identical to the pre-amendment run — the grid and the counted
  verdict are observationally invisible, and the ecc83 path stayed
  byte-frozen (no golden re-pinned).
- **The trigger reading cleared the line**: typical move
  **137.7 → 44.4 µs/probe** — under ADR-0016's 100 µs line for the first
  time, on the same protocol as the #63 re-run (one improve round on a
  true fixed point, slope over the derived probe scale). The
  acceptance-path average fell **162.2 → 70.7 µs/candidate**
  (6,164 → 14,487/s) — both levers at once: the grid cut the pair scan's
  `shapes_overlap` tests to the gathered neighbourhood, the counted
  verdict removed the per-probe canonical assembly.
- **Both rows moved for the same reason**: macro ratio
  **21,910× → 39,597×** (headline wall 229.51 → 97.66 s); ecc83 micro
  engine 101.5 → 56.9 µs, ratio **32,062× → 44,596×** — the small board's
  probes pay the same assembly tax, so the counted verdict shows there
  too. The wrapper absolutes moved with the machine only (kernel/OS
  unchanged between today's runs; the wrapper legs are real oracle work,
  2.73 s vs 3.55 s per candidate on tiny_tapeout).
- **The move mix is wall-share, not a threshold** (ADR-0019):
  1–2 pads 54% · 3–8 pads 10% · 9+ pads 36% — the passives' share fell
  with their probe cost, and the 9+ bucket's rose as the big parts
  became the expensive movers (their candidates span more cells to
  gather). Report-only, per the standing rule.
- **Comparability discipline**: the objective and candidate count are
  identical to the placements-fix receipt *by design* — the amendment
  changed the probe's cost, never its verdict. Any drift in those two
  numbers would have been the falsification gate firing.

The slow-tier suite (`uv run pytest -m slow`) is green on this tree:
the derived ratio clears the floor, the linearity receipt is recorded,
the headline replays byte for byte, and the pinned readings assert.

Reproduce with `uv run python -m net2board.examples.session --slow` —
the candidate count and objective are deterministic; the wall times and
ratios are machine-facts of the run that produced them.
