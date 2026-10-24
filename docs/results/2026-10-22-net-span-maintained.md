# The net-span term reads a maintained record — 2026-10-22 (#113)

The #113 dated receipt. ADR-0016's objective-deltas clause (amended this date) replaces the per-probe O(degree) NetSpan recompute — the term #106's amendment graduated, 29% of a 1–2-pad probe with ~21.2M list appends under it — with a per-net span record: per direction (min-x, max-x, min-y, max-y) the best value and the slot holding it plus the best value from any *other* slot (top-two-from-distinct-slots), so a probe answers best-or-runner-up in O(1) and folds in the moved slot's own candidate coordinates. `commit` — still the only writer — rebuilds the moved slot's nets' records whole at O(degree) on the write side, sharing one body with the entry build. The objective's full recompute (`_net_span_terms`) is untouched and stays the reference.

- **Command**: two detached `session --slow` runs (long-runs recipe), sequential on a quiet machine; the shares come from `profile tiny --scope "1-2 pads"` (the #106 instrument) on the same tree. Two earlier pairs of readings on intermediate trees of this ticket (79.3–93.9 µs/probe subject spread across four runs) agree with the final pair; the table records the final pair only — the runs that match the landed code.
- **Tree**: `dev` @ `c089da8` + this ticket's working-tree change (uncommitted at run time); the review's dedup and fold cleanups are in everything the table reports.
- **Provenance** (from the runs): Python 3.12.12 · Linux x86_64 (zen kernel) · kicad-cli 10.0.7 · `BUDGET_TINY` 2,500,000.

## The subject readings (acceptance criterion 1)

| Row | run one | run two | agreement |
|---|---|---|---|
| typical move (1–2 pads) µs/probe — the #112 subject | **94.5** | **82.5** | +14.5%, inside ADR-0026's ±20% |
| probes under the subject | 121,590 | 121,590 | identical |
| 3–8 pads µs/probe | 200.6 | 174.7 | +14.8% |
| 9+ pads µs/probe (report-only) | 687.1 | 570.4 | +20.5% |
| J4 µs/probe (report-only) | 1,997.9 | 1,651.3 | +21.0% |
| U6 µs/probe (report-only) | 1,110.2 | 905.3 | +22.6% |
| tiny wall s | 223.56 | 241.39 | +8.0% |

Both subject readings sit under the 100 µs bar — 104.2–121.4 on #112's readings before the change. `replay byte-equal: True` on both runs; the fast tier's pinned candidate counts and byte-frozen goldens held unchanged throughout (no re-pin, criterion 3). The ±20% WARNs the perf comparison prints against the 2026-10-20 history are this optimization itself, attributed here; the perf JSON (`docs/results/2026-10-22-perf.json`, written by run two) keeps its schema — prior files still read tolerantly (criterion 4).

## Where the probe's time goes now

The scope profile (1–2 pads, 121,590 probes, same tree): `_objective_changed` cumulative fell from 28.9% to ~8.5% of a probe. The residual is the record fold — four best-or-runner-up reads plus the moved pads' coordinates per term (`_extreme` over 2–3 integer candidates) — not a pin walk: the ~21.2M list appends under the old term are gone, and total profiler calls dropped 36.6M → 32.8M. The span term is no longer the top own-time row and no longer dominates: neighbour lookup (`_copper_query` + `_grid_query`) reads ~6.8%, the same order. The top row is `_pad_changed` — the pad finding records, the big-part lead #106's receipt already noted (34–38% on 9+/J4/U6) — report-only here, not a ticket.

## Reading notes

- Run one of the pair ran hot against run two across every row (subject +14.5%, report-only rows up to +22.6%): ambient load drifted between launches on this machine, and the report-only rows — smaller walls, same absolute noise — drift hardest. The subject's spread stays inside the pre-registered band, which is the bar; the over-band report-only rows are noise readings, not engine changes (identical probe counts, identical trees).
- The entry-board bench (`profile tiny --bench 20000`) reads flat against its 2026-10-22 pre-change reading (207 → ~209 µs/probe): the entry board has few placed pins, so the old walk was already short there. The subject was always the searched board's move mix.
- The record's write side prices O(degree) per commit — the pre-#113 probe's own recompute, moved to the write side, where commits are rare against probes. The `span_recomputes` count keeps its meaning (incident nets per probe), so the pinned complexity assertions are unchanged.
