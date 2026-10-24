# M3 results — 2026-10-24 (#92, the pad checks are Evaluator-native)

The #92 dated receipt — the one #85's sign-off re-measure reads. ADR-0016's doing as amended by #82: the three ADR-0022 pad checks are **Evaluator-native** — handled in `_handled`, never through `_foreign_changed`'s per-probe Board materialisation — over a per-side copper-grid pair, with drc's pure companions (#91's builders, one shared shorting+clearance pass since #98) kept as the bit-for-bit reference and the seeded-walk contract extended onto the dense synthetic board (exact-parity scenarios; `TestThePreFilterHidesNothing`'s exhaustive differential). `TestRegisteredChecksAreNative`'s strict xfail (`cd1b58c`) is gone — a probe materialises no Board for the pad checks. The native path landed across `27e423d`–`006adac` with the culls (`60c1fac`) and this ticket's count pins (`248b76a`); the readings ride the follow-ons the interval landed (#102's seam counts, #103's shared pre-filters, #105's named movers, #111's O(1) placement refs, #113's maintained span records).

- **Command**: bench first — `profile tiny --bench 20000`, 188 µs/probe on the tiny entry board (#113 read 207–209: flat, report-only) — then one detached pair, sequential on a quiet machine (long-runs recipe): `session --slow` (592 s of progress lines through `perf JSON written`, `session_exit=0`) then `pytest -m slow` (36 passed in 13:41, `slow_exit=0`).
- **Tree**: `dev` @ `248b76a` (this receipt uncommitted at run time).
- **Budgets**: `BUDGET_TINY` 2,500,000.
- **Provenance** (from the run): Python 3.12.12 · Linux x86_64 (zen kernel) · kicad-cli 10.0.7 · perf JSON `docs/results/2026-10-24-perf.json`.

## The trigger readings (ADR-0016/0019)

| Row | reading | bar |
|---|---|---|
| typical move (1–2 pads) | **81.5 µs/probe** (121,590 probes) | ≤ 100 µs — met (82.5 / 94.5 at #113) |
| macro ratio | **19,795×** | ≥ 1,000× floor — met (15,587× at #113) |
| micro ratio (ecc83) | **25,015×** (engine 120.2 µs/candidate) | ≥ 1,000× — met |
| replay byte-equal | True | required |
| fast tier | 960 passed, 31 s | ≤ 60 s ceiling (ADR-0026) |
| ecc83 fast-tier addition | ≈1.1 s since #98 | ADR-0019's sizing rule (≈≤2 s) — met |

tiny wall 190.26 s for 1,249,767 candidates (`no_further_improvement`, unmet `[]`, stranded `[]`); the candidate count is identical to 2026-10-22's history and replay is byte-equal — the walk did not move. Move mix (improve-round wall): 1–2 pads 34% (81.5 µs/probe) · 3–8 pads 10% (173.3) · 9+ pads 56% (613.0); named movers J4 (80 pads) 5.65 s (1,794.6 µs/probe) · U6 (61 pads) 3.05 s (966.9) — the big-part rows documented, not flattened, exactly the ticket's terms. ADR-0001 re-measures @150 placements: board 91.8 KiB, retained snapshot 0.22 KiB marginal, `with_placement` 6.3 µs.

## The culls' counted work (`248b76a`)

The cull commit (`60c1fac`) landed src-only; the owed count pins are in: a J4-shaped fixture (80-pad two-row connector) whose long neighbour's grown-box span covers 32 row pads — 64 pairs without the per-capsule cull, exact-priced only near its two pads; the edge-pricing count (a straddling mover prices exactly the 40 pads inside the rule band, a mover whole inside the shrunk outline prices nothing); and `_pad_at`'s first-match pin (duplicate pad locations resolve the first pad, the table per (footprint, turn, side)). Each verified red with its cull disabled.

## Reading notes

- The perf comparison's ±20% WARNs against 2026-10-22 (tiny wall −21.2%, macro ratio +27.0%, J4 +8.7%) are ambient drift between runs — the evaluator's probe path is unchanged since `c089da8`, candidates are identical, replay is byte-equal, and the 2026-10-22 receipt itself measured ±20.5–22.6% ambient spread on these rows.
- The micro row's engine reads 120.2 µs/candidate against the README's retired 63.5: the pad checks ride every candidate since #91/#92, and the ratio still drops from the retired 43,965× only because the wrapper baseline is unchanged while the engine grew — 25,015× is the honest number.
- The finding-descriptions deferral and its ADR-0009 amendment are **dropped** from this ticket (decided 2026-10-23): the trigger is met without them, and the #106/#113 receipts call the remaining big-part cost (the `_pad_changed` finding records, 34–38% of a big-part probe) report-only — "a receipt note, not a ticket". Should the big-part rows ever become a target, that is a new perf ticket under #106's amendment discipline.

## Related

- #85's sign-off re-measure reads this receipt. #70 (the map), #91 (the foreign path this retires), #98 (the shared pass), #106/#113 (the trigger's history).
- ADR-0016 (the amendment), ADR-0019 (the sizing rule), ADR-0022 (the checks), ADR-0024 (the re-measure clause), ADR-0026 (report-only discipline).
