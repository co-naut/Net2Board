# M3 results — 2026-10-07 (#73, the human baseline first-routed)

The tiny_tapeout human baseline's first routing measurement: the demo board's own placement (`tinytapeout-demo.kicad_pcb`, byte-verbatim, Apache-2.0) applied to the fixture substrate, exported as DSN, routed under the pinned freerouting-2.4.1 jar with the default `DsnRules`, and judged by kicad-cli DRC before and after the SES re-import. This is the yardstick M3's acceptance grilling (#78) measures the solver against. Every number below comes from one chain run on one machine; the slow-tier test (`tests/test_dsn_freerouting.py::TestHumanBaselineRoundTrip`) pins them.

## The headline

**The human placement does not route clean either.** Freerouting finished `COMPLETED` with **90 unrouted and 108 violations** — under the engine's default rules (0.25 mm width / 0.25 mm clearance / 0.8 mm via), nobody routes this board clean, solver included. The acceptance fallback #78 anticipated ("what if the human baseline routes badly?") is live with facts, not hypothetical: the no-worse-than comparison stands, but COMPLETED-with-0-unrouted is unreachable for the human baseline itself under these rules.

## Chain receipt

- **Command**: `uv run pytest -m slow tests/test_dsn_freerouting.py`
- **Tree**: Net2Board-dev `dev`, #73's reader + exporter pad-orientation fix (see below); `DsnRules()` defaults, no overrides
- **Provenance**: Python 3.12.12 · kicad-cli 10.0.6 · freerouting-2.4.1, jar sha256 `251101c3eeac22d7e7dfcf6796603279e5d1000283eb82d8f093780f7afc6aa9` · Linux x86_64, 8 threads · kicad_demo/tiny_tapeout/tinytapeout-demo.kicad_pcb sha256 `0d26bf9b6d561e32cf610d428541d6433e60b45e1838f34f73467e25b9144814`
- **Router wall clock**: 3,418.9 s (~57 min) on the measurement run; the committed slow-tier test reproduced every pin on a fresh chain (58:02)

## Router leg (verbatim from the log)

- `finished with state: COMPLETED`
- final score: **(90 unrouted and 108 violations)**
- fanout stage: 463 SMD pins needing fanout, 394 escaped (85.1%), 47.84 s
- the router narrowed tracks to **0.186 mm** against the DSN's 0.25 mm rule to get there — its own congestion trade-off, which kicad-cli then reads as 71 `track_width` violations against KiCad's 0.2 mm default minimum (our `.kicad_pcb` export declares no `(setup)`, so the judge uses its own rulebook — the judge-configuration question is #78's)

## DRC legs (kicad-cli 10.0.6, `--severity-all`)

Pre-route (the placement alone — the board the solver never sees copper on):

- **0 shorting_items, 0 clearance** — the placement renders clean
- 2 copper_edge_clearance (the demo's own edge-adjacent pads; the upstream board reads the same 2 under its own rules)
- 16 courtyards_overlap — our bounding-hull courtyard rendering (ADR-0015); the upstream board's true polygons read 8
- silk classes and 150 `lib_footprint_issues` (library-table noise, the documented kicad-cli gotcha) — not copper facts

Post-route (SES re-imported onto our export):

- **0 shorting_items** — the router introduces no shorts
- ratsnest **88 unconnected** (the router's tuple says 90 — the tuple counts connections, the ratsnest counts item pairs; 2 slack, unlike ecc83's exact map, pinned as measured)
- 1 clearance (0.102 mm actual), 71 track_width (0.186 vs 0.2 default), 9 via_dangling + 2 track_dangling — all routing remnants of the same unrouted population
- the placement's standing classes unchanged: 2 edge, 16 courtyard

## The exporter fix the first measurement forced (#73)

The first chain run read **42 phantom shorts on the human placement**. The probe trail, each step pinned on kicad-cli 10.0.6:

1. The upstream demo board DRCs clean of shorts — our render diverged.
2. Minimal repro: one TSSOP-16 at rot 0 is clean; at rot 90 it shorts its own pins (26 shorts + 26 mask bridges).
3. KiCad's loader rotates pad *positions* with the footprint but reads a missing pad `(at … rot)` token as world 0 — it does **not** let pad frames inherit the footprint's rotation, so token-less pads render axis-aligned at any placement angle, and 1.475 mm pads on 0.65 mm pitch overlap.
4. KiCad's own board writer reduces the pad token to the placement rotation alone and drops the library's pad-local angle entirely: the demo's U5 carries 270° library pads and writes none; its U3/U4/U7 embedded tokens are exactly their footprints' rotations.

The fix: `PadIR`/`Pad` carry the library pad-local rotation (`#75`'s geometry graduation inherits the fact), and `_pad_block` emits the world token — placement rotation, plus the back-side writer's own 180 (whose left-right mirror is already baked into the emitted local positions). DSN needed no change: its `place` form carries the rotation and Freerouting rotates the whole image. Golden regeneration (sanctioned `UPDATE_GOLDENS=1` path): ecc83's rot-90 R3 pads gain their tokens; the importer IR golden gains `local_rotation`; byte-diff limited to pad `(at …)` lines.

## Factual correction (2026-10-09, #75)

Step 4's reading of U5 was wrong. The fixture's `SOIC-8_5.23x5.23mm_P1.27mm.kicad_mod` is the only fixture footprint carrying `(net …)` entries: it was copied out of the demo board's U7 instance, which sits at −90°, and KiCad **board** files store pad angles absolute — U7's pads are written `270`, U5's (placed at 0°) token-less. Both instances' true library-local angle is 0; the "270° library pads" this document recorded were U7's board-absolute angle baked into a library file. Honouring 270 in pad geometry overlaps neighbouring pads by 0.33 mm (12 self-shorts on U5+U7); ignoring it gives the correct 0.67 mm gap. Consequences in ADR-0022: pad world geometry composes placement rotation + library-local rotation, the exporter gains the missing `+ local_rotation` term, and the fixture file is replaced by its upstream library form. The exporter's emission as fixed in #73 (placement rotation, +180 on the back side) is right today only because every referenced footprint's true local rotation is 0.

Re-confirmed 2026-10-13 (#81, the doing): fixture replaced by the U5 instance's library form; importer raises on pad angles off the 90° multiples (retiring the unreferenced `Valve_ECC-83-2` alternative); exporter composes `placement + local (+180 back)`. Rendered boards byte-stable — goldens report zero diffs — and the pre-route DRC leg reproduces this document's reading exactly: 0 shorting_items, 0 clearance, 2 copper_edge_clearance, 16 courtyards_overlap, 150 lib_footprint_issues.
