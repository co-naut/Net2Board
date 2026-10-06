# tiny_tapeout hard-placement fixture

Milestone 2's hard-placement fixture (#30): the first board in the repo
where placement is genuinely difficult, unlike `ecc83` (15 through-hole
parts on a 40x30 mm rectangle — a search toy that hides the O(n^2)
evaluation wall). Committed subset:

- `tinytapeout.net` — KiCad s-expression netlist (version "E"), generated
  from the demo schematic with `kicad-cli sch export netlist`
  (Eeschema 10.0.6).
- `footprints.pretty/*.kicad_mod` — the 33 distinct footprints the netlist
  resolves against, committed **byte-verbatim** from their upstream
  sources (table below).

The import path is `BoardSpec.from_files(tinytapeout.net, footprints.pretty,
outline, stackup)` with `outline` = the 104.5 x 81 mm bounding rectangle of
the demo board's `Edge.Cuts` (upstream has rounded corners; M1 outlines are
rectangles) and `stackup` = `("F.Cu", "In1.Cu", "In2.Cu", "B.Cu")`.

## Why this board

- **150 components, 114 nets, 4 copper layers, 470 SMD + 138 through-hole
  pads.** The SMD majority finally exercises the pad-geometry gap M1's
  all-through-hole fixture left untested; the through-hole remainder keeps
  that coverage.
- **Courtyards: 139 rectangles + 8 circles + 3 without** (fiducial-class
  parts). The rectangle set includes notched pin-1 courtyards from modern
  KiCad libraries, which import via the bounding-hull rule (ADR-0015).
- **Courtyard area sums to ~5,850 mm^2 of the 8,464 mm^2 board — a 69%
  fill ratio.** Non-overlapping placement is a real packing problem, not a
  formality.
- **139 courtyard-bearing parts = 9,591 unordered pairs.** At the measured
  ~0.76 us/pair, one full `run_drc`-style recomputation costs ~7 ms —
  the all-pairs wall that incremental evaluation (#27) exists to break,
  now visible at fixture scale.
- Real structure that constrains search: a 47.8 x 51.8 mm TT04 breakout
  courtyard dominating one end of the board, an RP2040 (QFN-56) with dense
  fanout nets, connector fields, and 0402/0603 passive swarms.

## Provenance and licensing

Source project: the `tiny_tapeout` demo shipped with KiCad 10.0.6
(`/usr/share/kicad/demos/tiny_tapeout`), **Apache-2.0** (the demo carries
its own `LICENSE.txt`).

| Files | Upstream source | License |
| --- | --- | --- |
| `PinSocket_2x06…`, `TT04_BREAKOUT_SMB`, `TT_BREAKOUT…`, `GCT_USB4500…`, `WL_S7DS…`, `418121270808`, `434121025816`, `SolderJumper-2…` (8 entries) | demo-local `tinytapeout-kicad-libs/footprints/TinyTapeout.pretty` | Apache-2.0 |
| `SOIC-8_5.23x5.23mm_P1.27mm` (1 entry) | extracted verbatim from `tinytapeout-demo.kicad_pcb` — the upstream library renamed this entry to `SOIC-8_5.3x5.3mm_P1.27mm`, and the demo board embeds the exact instance it uses | Apache-2.0 |
| remaining 24 entries (`C_*`, `R_*`, `PinHeader_*`, `QFN-56…`, `SOT-*`, `TSSOP-16…`, `Crystal_*`, `LED_*`, `MountingHole_*`, `Fiducial_*`, `Fuse_*`, `NetTie-*`, `Oscillator_*`, `TestPoint_*`) | KiCad official footprint libraries, as shipped with KiCad 10.0.6 | CC-BY-SA 4.0 |

Redistribution of the CC-BY-SA 4.0 subset requires this attribution, and
share-alike applies to those `.kicad_mod` files themselves — not to designs
or boards produced with them (see
<https://www.kicad.org/libraries/license/>). The Apache-2.0 files carry
that license's notices in the demo distribution. Net2Board's own code is
MIT; these fixture files keep their upstream terms.
