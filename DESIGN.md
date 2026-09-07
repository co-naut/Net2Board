# Net2Board

A Python engine that models the physical realization of a circuit — bridging netlist and circuit layout. Substrate-agnostic: it models any layered physical realization, not just PCBs. Built as a library to be driven programmatically (by an AI orchestrator or a human script), not a GUI. Every operation is a pure function returning a new snapshot.

---

## Design model

**The Board is the whole world.** A `Board` is one immutable snapshot of a circuit's physical realization: its outline, its layer stackup, the placed footprints, and the nets from the netlist. There is no global state and no side effects; nothing outside a `Board` matters to a `Board`. Milestone 1 keeps the aggregate deliberately small — net classes, keepouts, and design rules arrive in later milestones.

**Snapshots never change.** Every mutation — place or move a footprint — returns a new `Board`; the old one is untouched and comparable. Branch, backtrack, diff: two snapshots that look equal *are* equal, exactly, with no float drift anywhere (integer nanometres, below, are what make that promise cheap). Collections are plain tuples today; structural sharing is a later optimization, not a contract change.

**One placement operation.** `board.with_placement(ref, x, y, rotation, side)` places a footprint if the ref is new, or relocates it if the ref exists — one reversible call per decision. Rotations are 90° increments; sides are front/back copper. Anything invalid raises immediately with a message an orchestrator can act on.

**Integer geometry, exact answers.** All internal coordinates are integer nanometres; millimetres appear only when parsing and writing files. Two primitives cover milestone 1: an axis-aligned `Rectangle` and a `Circle` stored as centre plus radius-squared — chosen so every geometric predicate (does A overlap B?) is exact integer arithmetic, never floating point. Rotations are multiples of 90° and mirroring is exact, so a footprint's courtyard in world space is computed losslessly.

**Nets are contracts, not copper — for now.** A `Net` names the set of pins that must be connected. Milestone 1 models nets — so the export renders a ratsnest and `unrouted_net_names()` reports what remains — but not their copper: traces, vias, and zones are later milestones.

**Checks are pure functions.** `run_drc(board)` returns a deterministic, canonically-ordered tuple of violations. A violation says what's wrong, how badly (`error` / `warning`), and exactly which objects and where (`"placement:C3"`, `"placement:R1"`, both anchor points) — enough to act on without holding the board. Milestone 1 ships one check: courtyard overlap, strict-interior — touching edges or tangent points are fine, matching KiCad. Adding a check later is purely additive.

## Pipeline

`import → build → place/move → check → export`, every stage pure:

1. **Import** — `BoardSpec.from_files(netlist, footprint_dir, outline, stackup)` parses a KiCad netlist and footprint library into plain intermediate representations. Parsers own the file formats so the model never sees them; a second input format later means a second parser, not a model change.
2. **Build** — `build_board(spec)` is a pure IR → `Board` join. If a netlist pin has no matching pad, it fails fast with a structured error naming the ref and pin; nothing half-built escapes.
3. **Place/move** — `with_placement` upserts; invalid calls raise.
4. **Check** — `run_drc` for feedback; `unplaced_refs()` and `unrouted_net_names()` for progress and goal signals.
5. **Export** — serialize the `Board` to a canonical, byte-deterministic `.kicad_pcb` that KiCad 9/10 opens, with nets assigned (so the ratsnest renders) and courtyard graphics (so KiCad's own DRC can cross-check ours).

Byte-determinism is a contract, not a nicety: same board in, same bytes out — every ordering canonical, every millimetre rendered to exactly six decimals.

## Direction

Milestone 1 is the walking skeleton of a larger ambition: routing (traces, vias, zones), net classes and hierarchical design rules, keepouts, pluggable simulations (SI/PI/thermal/EM) feeding violations back into the loop, and additional EDA formats on both import and export. The value objects and seams above are shaped so each arrives as an addition, not a rewrite.
