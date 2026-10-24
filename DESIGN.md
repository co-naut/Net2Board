# Net2Board

A Python engine that turns *design intent* into *legal geometry*. An external orchestrator states relations ("these caps within 2 mm of U1's power pins", "the power section in this region"); the engine solves for exact integer coordinates that satisfy them, checks them exactly, and exports boards KiCad opens. Substrate-agnostic: it models any layered physical realization, not just PCBs. Built as a library to be driven programmatically (by an AI orchestrator or a human script), not a GUI. Every operation is a pure function returning a new snapshot.

---

## Design model

**The Board is the whole world.** A `Board` is one immutable snapshot of a circuit's physical realization: its outline, its layer stackup, the placed footprints, the nets from the netlist — and the placement intent: the Constraints and Groups stating where parts must or should go (ADR-0011). There is no global state and no side effects; nothing outside a `Board` matters to a `Board`. Routed copper stays outside the aggregate (ADR-0017).

**Snapshots never change.** Every mutation — place or move a footprint, state or withdraw a constraint — returns a new `Board`; the old one is untouched and comparable. Branch, backtrack, diff: two snapshots that look equal *are* equal, exactly, with no float drift anywhere (integer nanometres, below, are what make that promise cheap). Untouched contents are shared by reference; a new snapshot costs its placement spine, not its contents (ADR-0001 as amended).

**One upsert per decision.** `board.with_placement(ref, x, y, rotation, side)` places a footprint if the ref is new, or relocates it if the ref exists; `with_constraint` / `without_constraint` / `with_group` author intent the same way, so geometry and its obligations travel together through every branch and backtrack. Invalid input raises immediately with a message an orchestrator can act on; a legal-but-intent-breaking board is *reported* by the checks, never rejected by the mutation.

**Intent has two hardnesses.** A **Requirement** is pass/fail — checked exactly, never traded, never a cost term. A **Preference** only ranks among Requirement-legal boards, as an integer weight on nanometres of shortfall. One relation vocabulary serves both kinds (ADR-0011): Proximity, Region, EdgeMount, KeepTogether — plus Criticality, the net-weight Preference (ADR-0014).

**Derivations turn intent into geometry.** `solve(board, refs=None, *, budget, free_rotation=False)` searches for coordinates satisfying the Board's intent — placing the unplaced, improving Preferences, and with the knob on turning unlocked parts in exact 90° steps (ADR-0021, ADR-0025) — and ends in `legalize`, the public small-bounded repair derivation, whose move set covers the pad-copper findings too: pairs push past clearance, edge findings clamp inside the shrunk outline (ADR-0012 as amended by M3); both are pure Board-in/Board-out functions, deterministic given their arguments, reporting what they could not satisfy by name. The Budget counts candidates evaluated, never seconds — a rotation probe costs a position probe. Inside a call, a private flat Evaluator answers legality and cost for single-Pose moves, bit-for-bit identical to `run_drc` and `evaluate` (ADR-0016).

**The objective is a pure function.** `evaluate(board)` ranks Requirement-legal boards in exact integer nanometres, itemized per term: per-net NetSpan (the field's HPWL over placed pad centers) as the weight-1 numeraire, every bounded relation a hinge that is exactly zero when satisfied. It reads intent off the Board — no objective argument, no hidden engine weights; every tunable is a Preference weight already on the snapshot (ADR-0014).

**Legal is defined by reference.** A board is legal when `run_drc` reports no Violation of error severity — so checker, repairer, and solver cannot drift into two notions of correctness, and every check registered in `CHECKS` joins the legality set for free. The checks are courtyard overlap (strict-interior; touching is fine, matching KiCad), the four constraint checks, outline containment at error severity with per-edge EdgeMount exemption (ADR-0018), and the three pad-copper checks — `shorting_items`, `clearance`, `copper_edge_clearance` — priced off the Board's `DesignRules` with KiCad's own type strings (ADR-0022).

**Integer geometry, exact answers.** All internal coordinates are integer nanometres; millimetres appear only when parsing and writing files. Two courtyard primitives — an axis-aligned `Rectangle` and a `Circle` as centre plus radius-squared — make every geometric predicate exact integer arithmetic, never floating point. Rotations are 90° increments and mirroring is exact, so a footprint's courtyard in world space is computed losslessly.

**Nets are contracts, not copper — and routing is delegated.** A `Net` names the set of pins that must be connected; `unrouted_net_names()` reports what remains. The engine's entire routing surface is a pure Specctra DSN writer, `export_dsn(board, sizes=RouteSizes())` (ADR-0017 as narrowed by ADR-0020/0024): the clearances are **not** a router argument — they are Board state (`DesignRules`, the one rules set legality depends on), and both numbers ride the handoff, with an outline clearance class that overrides the router's built-in edge default in both directions (#84's probe). `RouteSizes` carries only what the router may lay: one track width, one via padstack. Freerouting owns the jar, the SES, and the completion log; routed copper re-enters the loop as *intent* — Criticality weights, Proximity bounds — never as Board state. Boards that route: the M3 benchmark routes the human and the solver placements under these identical rules and compares the router's unrouted counts (ADR-0024).

## Pipeline

`import → build → place/state intent → solve → check → export`, every stage pure:

1. **Import** — `BoardSpec.from_files(netlist, footprint_dir, outline, stackup)` parses a KiCad netlist and real footprint libraries into plain intermediate representations; notched real-library courtyards parse as conservative axis-aligned hulls (ADR-0015). Parsers own the file formats so the model never sees them; a second input format later means a second parser, not a model change.
2. **Build** — `build_board(spec)` is a pure IR → `Board` join. If a netlist pin has no matching pad, it fails fast with a structured error naming the ref and pin; nothing half-built escapes.
3. **Place / state intent** — `with_placement` upserts geometry; `with_constraint` and friends upsert obligations; invalid calls raise.
4. **Solve** — `solve` searches within the candidate Budget and ends in `legalize`; `evaluate` reports the objective of any board.
5. **Check** — `run_drc` for exact feedback; `unplaced_refs()` and `unrouted_net_names()` for progress and goal signals.
6. **Export** — serialize the `Board` to a canonical, byte-deterministic `.kicad_pcb` that KiCad opens, with nets assigned and reference designators (so KiCad's own DRC can cross-check ours and a routed SES ingests by ref), and to canonical DSN for Freerouting.

Byte-determinism is a contract, not a nicety: same board in, same bytes out — every ordering canonical, every millimetre rendered to exactly six decimals.

## Direction

The division of labour is the product. The orchestrator — human or LLM — is good at relations, groupings, and judging tradeoffs, and is bad and expensive at exact coordinates and simultaneous geometric satisfaction. So the orchestrator stays in the outer loop (tens of calls: state intent, spend a budget, judge the outcome) and Net2Board owns the inner loop (thousands of exact candidate evaluations per step, with no oracle call in the path). Placement solving is where Net2Board differentiates. Routing is delegated to Freerouting, not searched. `kicad-cli` remains the DRC sign-off authority — our checks are the fast, exact ranking function it cross-validates. Not aimed at: being a KiCad wrapper or replacement, an end-user product, or a simulation platform.
