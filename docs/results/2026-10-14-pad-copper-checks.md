# M3 results — 2026-10-14 (#91, pad copper and the three checks)

ADR-0022's model and checks, full path: every copper-bearing pad's outline is a capsule (axis-aligned core rectangle grown by a radius, exact for rect/circle/oval/roundrect, an importer-hulled bounding rectangle for custom), priced pairwise against `board.design_rules` and against the outline edge lines — `shorting_items`, `clearance`, `copper_edge_clearance` at ERROR, type strings colliding with the oracle's. The parity receipt below is the ticket's acceptance: per-type parity on pre-route boards, pads being the only copper there.

- **Command**: `uv run pytest tests/ -m "not slow and not route"`, then `uv run pytest tests/ -m slow`, then `uv run pytest "tests/test_dsn_freerouting.py::TestDsnEdgeClearancePrecedence" -m route`
- **Tree**: Net2Board-dev `dev` @ the five #91 commits (`04fdc46` geometry capsules · `7261153` import · `acf7030` DesignRules · `4080d00` checks · `1ef56d0` export), plus this ticket's parity tests
- **Provenance**: Python 3.12 · kicad-cli 10.0.7 · KiCad demo fixtures committed (`kicad_demo/`), byte-verbatim upstream

## Readings

Per-type counts, ours (`run_drc`) vs kicad-cli (`pcb drc --severity-all`, which reads the fresh-project 0.2/0.5 mm defaults our export lets it read — the #78 judge invariant):

| board | checker | shorting_items | clearance | copper_edge_clearance |
|-------|---------|----------------:|----------:|----------------------:|
| tiny_tapeout demo placement | ours | 0 | 0 | 2 |
| tiny_tapeout demo placement | oracle | 0 | 0 | 2 |
| demo + C1 co-placed onto C2 (deliberately shorted) | ours | 3 | 0 | 2 |
| demo + C1 co-placed onto C2 | oracle | 3 | 0 | 2 |
| ecc83 acceptance board (clean) | ours | 0 | 0 | 0 |
| ecc83 acceptance board (clean) | oracle | 0 | 0 | **9** |

- **The demo's 0/0/2 is the C12 pair** — `placement:C12` pads 1 and 2, each reported at **gap 490000 nm** against the 500000 nm edge rule: the #73 receipt's "0.49 mm actual", reproduced to the nanometre by the isqrt-floored capsule gap. The description wording pins it (`Copper edge clearance: placement:C12 pad 1 (gap 490000 nm, rule 500000 nm)`).
- **The shorted variant's granularity is exact because the parts are SMD**: one finding per pad pair, and KiCad counts the same pairs. On THT shorts the counts split — the ecc83 beat board (two coincident plates) reads 2 ours vs 4 oracle, KiCad counting per copper layer where the model counts per pad pair (the ADR-0022 finding shape: no `pad:` ref kind, one finding per pair, locations at the pad world centres). Recorded as the model's granularity, not chased.
- **The ecc83 edge divergence is the receipt's one pinned divergence** (ADR-0022's posture): KiCad has no EdgeMount concept and reports the near-edge pads; we exempt them by stated intent — the board's thirteen Requirement EdgeMounts, ADR-0018's per-edge mechanism extended to pad copper. The oracle's 9 is measured under the judged rules; ADR-0022's charted "eleven" was counted against the demo project's own 0.3 mm edge rule, which the judge does not read.

## The emission and the rename

`export_dsn` now reads `board.design_rules` for the general `(clearance …)` in both rule blocks, names the outline's `(clearance_class edge)`, and prices every item class against it — `wire/via/smd/pin/area × edge`, named pairs after the general clearance, values always present (#84's three traps; the reader splits pair tokens on `_`, so the class name stays underscore-free). The route-tier pin (`TestDsnEdgeClearancePrecedence`) now runs on the production export — the 300 µm class routes where the built-in 500 µm blocks and the 800 µm class blocks where it routes, with one injected leg kept to pin the unpriced-class zeroing trap the emitter must never reproduce. `DsnRules` is renamed `RouteSizes` (width + via only) in the same breaking release as the clearance removal, per ADR-0024 — no alias.

`.kicad_pcb` export emits `(roundrect_rratio …)` for roundrect pads: the checker prices the shrunk core, so the render must round the same corners or the oracle judges a different pad than the model checks.

## Consequences

- **Legality is now pad-copper-aware**, and the walks feel it: the counted verdict's error counts include the three copper findings, so candidate ranking changed and every pinned walk re-derived. The slow-tier re-pins land with this receipt (below). The Evaluator's finding key gained the locations (several pad pairs share a placement pair; an intra-footprint pair repeats the ref — pad numbers repeat, the pad world centres do not), and the fast paths drop findings by stored key.
- **Named no longer implies restored** (ADR-0012 as amended by #83): legalize parks a finding whose type has no repair vocabulary without restoring the offender — an unrepairable copper finding must not undo the part's successful repairs of other findings. The ecc83 beat no longer settles to clean: its first strictly-better move trades the overlap-plus-two-shorts cluster for a single new overlap one part over, the attempt cap ends the bounce, and the parts are named — #95's clearance moves (aiming past full clearance, killing the equal-count trade) are what settle that board. _(Corrected 2026-10-23, measured in the #95 receipt: the moves never get their turn — the bounce is courtyard-to-courtyard, every escape from U1 lands back on R2, and the outcome is unchanged; the pin now says so.)_
- **The incremental cost is the next ticket's** (#92): the three checks ride the Evaluator's foreign path with companions declared — correct per probe, all-pairs at entry — and the ADR-0016 trigger (≤100 µs typical move, ≥1,000× floor) re-measures against #92's native grids.

## Slow-tier re-pins: deferred to #92

The three copper checks ride the Evaluator's foreign path until #92 — correct per probe, but every probe pays the companion scans (mover pads × all board pads, pure Python), which puts the tiny session's ~1.5 M candidates at hours of wall time. The pinned readings the walks decide (candidate counts, headline bytes, the README Measured rows) are a function of the verdicts alone, and #92's contract is bit-for-bit identical findings to these pure companions — so the walks cannot shift between this ticket and #92. One slow-tier run after #92 re-pins for both, at native speed. This ticket's tier evidence: the unit tier green (855, the ecc83 session included — solve → legalize → export with the checks live), the parity oracle runs above, and the route-tier precedence pin on the production emission.
