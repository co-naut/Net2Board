# M3 results — 2026-10-24 (#85, the solver's placement first-routed)

The solver's own tiny_tapeout placement, routed for the first time: ADR-0023's trigger (a) — the fact nobody had measured — read under the same chain, the same sha-pinned freerouting-2.4.1 jar, and the same shipped exporter as the human row. Two solver runs, one harness: knob off (the sign-off face, ADR-0024's three clauses) and knob on (the rotation search's first routed reading, ADR-0025's sizing datum), with the human baseline re-measured alongside — its pinned tuple predated the exporter (read at 0.25 mm in #73, before ADR-0020's 0.2 mm default and ADR-0022's edge class shipped), and same-rules comparison is the whole point. Every number below comes from these chain runs on one machine; the route-tier tests (`tests/test_dsn_freerouting.py`) pin them and reproduced every pin on fresh chains.

## The headline

**Solver unrouted ≤ human: ADR-0023's trigger (a) closes by measurement.** The knob-off solver placement routes COMPLETED with **61 unrouted and 18 violations**; the re-measured human baseline reads **(71, 45)**. The load-bearing kill condition held: the congestion question closes without the falsification experiment graduating, and nothing adopts a term. The solver board is also cleaner than the routed human board on every enforced type after routing — 0 shorts, 0 crossings, 0 clearance, 0 edge against the human's 3 / 0 / 1 / 2 — and shows zero enforced copper findings before routing under both `run_drc` (empty) and kicad-cli.

## Chain receipt

- **Commands**: three detached runs of `uv run pytest -m route tests/test_dsn_freerouting.py` — (a) `NET2BOARD_REMEASURE_HUMAN=1 -k Human` (1:06:43), (b) `NET2BOARD_REMEASURE_HUMAN=1` full module (1:10:39, 28 passed / 3 skipped), (c) `NET2BOARD_ROUTE_KNOB_ON=1 -k KnobOn`, three attempts: the first killed by the 5400 s router leash (the SES never written; the fixture then learned to dump the solve reading before routing and to carry a 3 h leash — `KNOB_ON_LEASH`), the second completed with all readings (1:52:18, one fixture type-slip in the strand test: `[]` dumped against a `()` compare), the third the same chain re-run to green after the fix (1:37:43 — the solve replayed byte-identical, the router read a different tuple on the same DSN: run-to-run variance, reported in run 2). The human chain re-measured in (a), re-pinned, and reproduced fresh in (b); the solver chain ran in both (a) (via two clause tests its `-k` pattern also matched) and (b) — byte-identical readings.
- **Tree**: Net2Board-dev `dev` @ `51f9149` plus #85's test additions (this ticket's working tree at run time; the human constants were re-pinned from (a) before (b) launched).
- **Provenance**: Python 3.12.12 · pytest 9.1.1 · kicad-cli 10.0.7 (#73 measured at 10.0.6) · openjdk 27.0.1 · freerouting-2.4.1, jar sha256 `251101c3eeac22d7e7dfcf6796603279e5d1000283eb82d8f093780f7afc6aa9` · Linux x86_64, 8 threads · kicad_demo/tiny_tapeout/tinytapeout-demo.kicad_pcb sha256 `0d26bf9b6d561e32cf610d428541d6433e60b45e1838f34f73467e25b9144814` (byte-identical to #73's).
- **Rules, both placements**: `DesignRules()` defaults (clearance 0.2 mm, edge 0.5 mm) and `RouteSizes()` defaults (width 0.25 mm, via 0.8 mm), carried by the production `export_dsn` — the outline `clearance_class` rides the DSN (ADR-0020/0022). The human row is the demo's placement under our rules, freshly measured, not the demo's routed board.
- **Router invocation** (identical for every run): `java -jar <jar> -de <dsn> -do <ses> --gui.enabled=false --api_server.enabled=false -da --user_data_path=<ud>`, wall leash 5400 s; SES placement echo stripped before `pcbnew.ImportSpecctraSES`; kicad-cli `pcb drc --format json --severity-all` before and after.
- **Walls** (fixture granularity, ADR-0026 report-only; the route leg dominates — derived, ≈ fixture wall − ~4 min session − ~45 s re-import/DRC legs): human chain 1,754 s (a) / 1,839.7 s (b); solver chain 2,248.7 s (a) / 2,352.9 s (b) including the ~4 min session. The full sign-off module with the human gate on runs ~71 min.
- **Entry-board bench before the runs**: 193 µs/probe (20,000 probes, 3.86 s) — consistent with this week's tiny walls (152–180 µs/probe effective, #92/#113 receipts); the 81.5 µs/probe figure is the searched-board typical-move bucket, a different subject. No alarm; the solve added ~4 min.

## The human re-measure (freshly measured, then re-pinned)

**The shipped rules route the human board better than #73's did**: COMPLETED with 71 unrouted and 45 violations against (90, 108) at 0.25 mm — the narrower clearance gives the router more freedom. Two readings moved with it, both pinned as measured rather than smoothed:

- **The router now shorts 3 nets of its own** (0 at 0.25 mm) — post-route `shorting_items` 3, plus 1 `clearance`, 98 `track_width` (report-only), 19 `via_dangling` + 3 `track_dangling`. Clause 3's yardstick is this board as measured: `HUMAN_POST_ENFORCED` = shorting 3 / crossings 0 / clearance 1 / edge 2.
- **The ratsnest maps exactly here** (71 post-route `unconnected_items` = the tuple's 71); #73's 2-slack reading was not a law — the solver board below reads 2 slack again (59 against 61).

Pre-route the placement renders clean of shorts and clearance under the shipped exporter (the judge invariant holds: 0 / 0, with the demo's standing 2 `copper_edge_clearance` and 16 `courtyards_overlap` unchanged, plus silk/library noise classes). The fresh chain in (b) reproduced every re-pinned number.

## Run 1 — the solver's placement, knob off

The ADR-0019 slow-tier tiny session as shipped (no authored intent, `free_rotation` off), routed fresh at sign-off:

- **Solve**: 1,249,767 candidates (the pinned `TINY_ENTRY_CANDIDATES`, exactly), stop `NO_FURTHER_IMPROVEMENT`, Objective total **5,922,911,847 nm** (5.923 m), strand reading empty.
- **Router**: `finished with state: COMPLETED`, final score **(61 unrouted and 18 violations)**; ratsnest 59 after import (2 slack, the tiny reading again); 145 `track_width` post-route (report-only).
- **Clause 1** (enforced): 61 ≤ 71 — passes.
- **Clause 2** (enforced, both judges): `run_drc` returns **zero findings of any type**; kicad-cli pre-route shows zero enforced copper findings (only the silk `silk_over_copper` 199 / `silk_edge_clearance` 4 and 150 `lib_footprint_issues` noise classes — and unlike the human board, not even an edge finding: the search kept every part off the edge band).
- **Clause 3** (enforced): post-route enforced types all read 0 against the human's {3, 0, 1, 2} — the routed solver board is cleaner than the routed human board on every enforced type.
- **Reported, never enforced**: state `COMPLETED`; router violations 18 (against the human's 45); the import leg silently skipped nothing.
- **Determinism**: the (a) and (b) chains read byte-identical — same tuple, same candidate count, same Objective total — on fresh chains; no tolerance margin needed, nothing blocked.

**The decision**: solver unrouted (61) ≤ human (71). ADR-0023's trigger (a) does **not** fire: the routability question closes by measurement, the falsification experiment does not graduate from the map's fog, and no term enters the objective. Placement-caused copper findings were checked first regardless (clause 2 above): there are none to blame on anything.

## Run 2 — the knob on (ADR-0025's first routed reading)

The same chain with `free_rotation=True`, solved directly under a one-off 10M ceiling (the harness's pinned `_step` face pins the knob-off stop reason; this run's stop reason is the reading):

- **Solve**: **8,245,534 candidates**, stop `NO_FURTHER_IMPROVEMENT` — the search converged, not exhausted, at 2.06× the ceiling and ~2× ADR-0025's ≈3–4M pre-registration. Solve wall 1,410.5 s (~23.5 min, ~171 µs/probe effective). Objective total **5,210,274,939 nm** (5.210 m) against the knob-off headline's 5.923 m — rotation bought a **12% shorter wirelength**. Strand reading empty: nothing the rotation search placed had to stay unplaced.
- **Router**: `finished with state: COMPLETED` on both completions — final score **(81 unrouted and 28 violations)** (attempt 2, route wall ≈ 5,330 s ≈ 89 min) and **(79 unrouted and 28 violations)** (attempt 3, route wall ≈ 4,450 s ≈ 74 min) — ratsnest 78 and 72 after import. **The input DSN is byte-identical across the two completions (sha256 `ce20a226…`), so this is run-to-run variance in the router itself**, at whatever time-budgeted margin a ~75–89 min route runs against; the pinned faces (human, knob-off, ecc83) reproduced exactly on their fresh chains, so the pin discipline is untouched — nothing pins the knob-on face. The first attempt of this run was killed by the human-sized 5400 s leash before writing the SES, missing completion by roughly a minute. **The board a rotation chose is a materially harder route than the knob-off board's ~30 min on every attempt.**
- **Copper, both judges**: zero enforced findings before routing (`run_drc` empty; kicad-cli reads only the silk/library noise classes) and zero after — 0 shorts, 0 crossings, 0 clearance, 0 edge, with 146 `track_width` and 10 `via_dangling` report-only.
- **Reported, never enforced**: state `COMPLETED`; nothing here adopts a term.

**The reading**: rotation improved the engine's objective (−12% wirelength) and produced a board that routes **worse** — 79–81 unrouted across two completions against the knob-off 61 and the human 71, at 3–3.5× the router wall — and whose tuple moves run-to-run on a byte-identical input. Wirelength-optimality is not routability, which is ADR-0023's thesis in one measurement: the datum feeds the congestion question's evidence (and the map's EdgeMount-facing fog discussion — a rotated connector is exactly the part whose facing an orchestrator vision pass would name), but it fires no trigger and pins nothing. The `BUDGET_TINY` sizing question settles: the knob-off headline keeps its 2.5M pin; a knob-on tiny face, if the M3 spec ever asks for one, re-pins at ≥8.3M — recorded here, not re-pinned.

## Consequences recorded elsewhere

- **ADR-0024's preview clause is moot, amended in the same commit**: the re-measure ran under the shipped exporter — no `DsnRules(clearance_nm=200_000)` preview, no re-pin cycle; #73's pins were simply re-measured and re-pinned once, here.
- `docs/agents/long-runs.md`'s route-tier walls updated (the ~57 min human figure was a 0.25 mm reading).
- Raw DSN/SES/routed files stay uncommitted — determinism regenerates them from the tree; the chains' `*_reading.json` dumps are the per-run record the tests' fixtures write.
