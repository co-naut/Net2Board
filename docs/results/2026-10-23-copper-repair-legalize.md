# M3 results — 2026-10-23 (#95, legalize repairs the copper findings)

ADR-0012 as amended by #83 lands its repair vocabulary: `shorting_items` and `clearance` push the mover along one axis until every violating pad pair between the two refs sits `clearance_nm + _MARGIN_NM` apart — the largest shift per direction across the checker's own records, read off the Evaluator's stored findings (`Evaluator.pad_pairs`, the #91 obligation's read half) — and `copper_edge_clearance` clamps the mover's whole copper box inside the outline shrunk by `edge_clearance_nm + _MARGIN_NM` per unstated edge, a Requirement EdgeMount's stated edge exempt, a Preference exempting nothing. Intra-footprint pairs have no candidates and park with the part movable; the report stays the final board's (#96). Fast tier green (955 passed), the two slow oracle receipts below.

- **Command**: `uv run pytest tests/ -m "not slow and not route"`, then `uv run pytest "tests/test_oracle_tinytapeout.py::TestLegalizeRepairsCopper" -m slow` (detached, the long-runs recipe)
- **Tree**: Net2Board-dev `dev` @ this ticket's commits, on top of `0dacd73`
- **Provenance**: Python 3.12 · kicad-cli 10.0.7

## Readings

The tiny demo placement legalizes with 27 movers — the C12 pair's 0.49 mm edge gap pulls in by exactly 11 000 nm (the 10 nm deficit + the 1 µm margin), every pad-copper finding clears, and the residuals are the board's authored facts alone: 6 courtyard_outside_outline + 5 courtyards_overlap, all on edge-hanging connectors (J1, J3, J5, J6, J8, J12–J14, J17), unrepairable by translation and named. kicad-cli reads the same board 0/0/0 on the three copper types; the deliberately shorted variant (C1 co-placed onto C2, legalized) also reads 0/0/0 at the oracle.

| board (legalized) | checker | shorting_items | clearance | copper_edge_clearance |
|-------|---------|----------------:|----------:|----------------------:|
| tiny_tapeout demo placement | ours | 0 | 0 | 0 |
| tiny_tapeout demo placement | oracle | 0 | 0 | 0 |
| demo + C1 co-placed onto C2 | ours | 0 | 0 | 0 |
| demo + C1 co-placed onto C2 | oracle | 0 | 0 | 0 |

- **Bench, flat**: `profile tiny --bench 20000` reads 260 µs/probe with this tree, 263 µs/probe at the base commit `0dacd73` minutes apart — the probe path is untouched (the read is legalize-only), and today's ~260 vs the 2026-10-22 receipt's ~209 is machine drift both trees share, not a regression.
- **The ecc83-beat correction**: the 2026-10-14 receipt predicted #95's clearance moves would settle the beat board. Measured now: they never get their turn. The bounce is courtyard-to-courtyard — the repair's first strictly-better move trades the overlap-plus-two-shorts cluster for a single new overlap on U1, and from there every escape from U1 lands back on R2; the closed forms are per-finding, so no candidate walks the diagonal away from both. The caps end it, both parts restore and stay named — the outcome is unchanged from #91's reading, and the pin (`test_ecc83_overlap_beat_legalizes`) now says so.
- **Per finding, one direction at a time**: a pair finding's four per-direction shifts are aggregated across *every* violating pad pair between the two refs, so a short's repair aims past full clearance and cannot be "cleared" into an equal-count `clearance` finding. Measured on the co-placed short: the repair ends clean in two −y moves — the touching outer pads' clearance (0.201 mm), then the yard push (1.1 mm), final pad-box gap 0.801 mm — with nothing left to bounce on; the fast synthetic pins (`TestCopperRepairs`) hold the single-move arithmetic where one family fires (0.1 mm gap repairs in one 0.101 mm move).
