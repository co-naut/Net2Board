# M3 results — 2026-10-13 (#89, Pose plumbing — the Evaluator's one operation typed, knob off)

The #89 dated receipt. ADR-0025's knob-off half landed as a pure refactor: a private `Pose(x, y, rotation)` NamedTuple is the one operation's argument — `probe`/`probe_key`/`commit` take `(ref, pose)`, the candidate families yield Poses at the standing rotation (the first-fit grid at 0), the family dedup key is the Pose, `_originals` stores Poses in both `solve` and `legalize`, and the tie-break key extends to `(x, y, rotation)`. Finalize's skip compares Poses, fixing the latent defect where a part turned in place would have been silently dropped from the result. The courtyard grid gains ADR-0025's reworded invariant — "slots never unplace and never change side; a rotation change re-buckets like a move" — and a fast-tier test pins it: the WIDE yard's 10×4 mm elongated boxes re-bucketed across the dense board's 1 mm cells at 0°→90°, probed directionally both ways (the new column finds the overlap, the abandoned row finds none).

No behaviour change is the deliverable, and byte-stability is the proof that the plumbing is pure: knob off, every Pose carries its ref's standing rotation, so each candidate `Placement` is exactly the pre-Pose one — the dedup collisions, the probe order, the total order on ties, and the finalize/legalize equality semantics are unchanged. The knob itself is #90's.

- **Command**: `uv run pytest -q`, then `uv run pytest -m slow -q`
- **Tree**: Net2Board-dev `dev` @ `18cde9e` + #89's change (uncommitted at run time)
- **Fast tier**: 756 passed, 51 deselected, 14.7 s — every golden (`tests/goldens/` drc, export, import, session) byte-identical, zero churn; the new re-bucketing test is the one added test.
- **Slow tier**: 30 passed, 777 deselected, 689.95 s (11:29 — inside the 2026-10-13 remeasure's 11:38 band): `TINY_ENTRY_CANDIDATES` 1,414,784 and `TINY_HEADLINE_CANDIDATES` 164,019 re-pinned untouched, the ecc83 session replay (`tests/goldens/session/ecc83_session.json` + `.kicad_pcb`) byte-identical, candidate counts per step unchanged.
- **Provenance**: Python 3.12.12 · ruff 0.16.3 (format + check clean)
