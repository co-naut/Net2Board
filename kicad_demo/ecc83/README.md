# ecc83 test fixture

Milestone 1's acceptance fixture for Net2Board. Committed subset:

- `ecc83-pp.net` — KiCad s-expression netlist (version "E"), generated from the
  demo schematic with `kicad-cli sch export netlist` (Eeschema 10.0.5).
  15 components, 13 nets, 33 nodes, 6 libparts.
- `footprints.pretty/*.kicad_mod` — the 10 footprints the netlist resolves
  against (project library, prefix `Footprints:`).

Everything else in this directory (the original demo project files, 3D shapes,
board files) is intentionally not tracked.

## Provenance and licensing

Derived from KiCad's bundled `ecc83` demo (found in the KiCad source tree
under `demos/ecc83`) and from KiCad's official footprint libraries:

- The demo project is distributed with the KiCad sources (GPL-3.0-or-later).
- The footprint libraries are distributed under CC-BY-SA 4.0; redistributing
  this subset of the libraries requires this attribution, and the
  share-alike terms apply to the redistributed footprint files themselves
  (not to designs or boards produced with them — see
  <https://www.kicad.org/libraries/license/>).

Net2Board's own code is MIT; these fixture files keep their upstream terms.
