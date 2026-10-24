# M3 results — 2026-10-13 (#84, the DSN edge-clearance precedence pin)

The probe ADR-0020 (as amended by #75) deferred to this ticket: does Freerouting 2.4.1 *actually* route against the DSN-named outline clearance class, or does `HeadlessBoardManager.applyCopperToEdge- ClearanceOverride`'s built-in 500 µm (`DEFAULT_COPPER_TO_EDGE_- CLEARANCE_UM`) win anyway? ADR-0022's "both numbers reach the router by construction" was source-read, never jar-pinned — without this pin the DSN edge number was faith. The verdict: **the emitted class wins, in both directions** — a 300 µm class routes geometry the 500 µm default blocks, and an 800 µm class blocks geometry the default routes. The pin lives as `TestDsnEdgeClearancePrecedence` in `tests/test_dsn_freerouting.py` (route tier — the ADRs' "slow-tier pin" wording predates ADR-0024's tier split; the slow tier is defined as no-routing). The probe also pinned two reader traps the #91 emission must respect, and one router-internal counting quirk.

- **Command**: the scratch sweep under `/tmp` (five legs, ~2 s each), then `uv run pytest tests/test_dsn_freerouting.py::TestDsnEdgeClearancePrecedence -m route`
- **Tree**: Net2Board-dev `dev` @ `726aebd` + this ticket's test (uncommitted at run time)
- **Provenance**: Python 3.12.12 · kicad-cli 10.0.7 · freerouting-2.4.1, jar sha256 `251101c3eeac22d7e7dfcf6796603279e5d1000283eb82d8f093780f7afc6aa9` · OpenJDK 27 · Linux 7.2.9-zen1-1-zen x86_64

## Method

Throwaway geometry whose routability flips on the edge number alone: two 0.5 mm SMD rects sharing one net, hanging off the bottom edge of a 20 mm-wide strip. With the edge requirement at 500 µm on a 1.2 mm strip, the horizontal trace band is empty (trace centre would have to sit in [625 µm, 575 µm]) and the net cannot route; at 300 µm the straight shot is legal; on a 1.6 mm strip at 800 µm the band closes again. The DSN is the real `export_dsn` output with the class string- injected — the production shape plus the delta #91 will land — and each leg is a bare route (`java -jar`, headless, the pinned jar), read through the log's `(N unrouted and M violations)` tuple. No SES, no DRC: the question is router-internal.

The emission under test, on the structure rule (the named pairs *after* the general `(clearance ...)` — see Traps):

```text
(boundary
    (path pcb 0 ...)
    (clearance_class edge)                      <- the outline's class name
)
(rule
    (width 250)
    (clearance 250)
    (clearance 300 (type wire_edge))            <- every item class vs edge,
    (clearance 300 (type via_edge))              mirroring the override's own
    (clearance 300 (type smd_edge))              all-classes loop; the pair
    (clearance 300 (type pin_edge))              token is <item>_<name>,
    (clearance 300 (type area_edge))             split on "_" by the reader
)
```

## Readings (verbatim tuples)

| leg | strip | pads off edge | DSN | state | unrouted | violations |
|-----|-------|---------------|-----|-------|----------|------------|
| B | 1.2 mm | 350 µm | untouched | COMPLETED | 1 | 2 |
| A | 1.2 mm | 350 µm | class edge @ 300 | COMPLETED | 0 | 2 |
| D | 1.2 mm | 350 µm | class named, unpriced | COMPLETED | 0 | **0** |
| Cc | 1.6 mm | 550 µm | untouched | COMPLETED | 0 | 2 |
| C | 1.6 mm | 550 µm | class edge @ 800 | COMPLETED | 1 | 2 |

- **B vs A — the ticket's question, answered**: identical geometry; without the class the 500 µm default leaves the net unroutable, with the 300 µm class it routes. The emitted class replaced the built-in.
- **Cc vs C — the reverse direction**: geometry the default routes becomes unroutable under the honoured 800 µm — the class is not parsed-then-clamped to the default.
- **A gap-sweep** (lone-pad boards, no net) located the *pad-violation* threshold under the 500 µm default in (575, 600] µm — the outline keepout behaves ~100 µm fatter than the matrix value (candidate causes read in source: `BoardOutline.HALF_WIDTH` is only 10 µm, so something else grows it; not chased further). The pad violations in legs A/B/Cc/C are this quirk, not a treatment effect — routedness is the discriminator, and the router *blocks routing* at exactly the matrix value (450 µm pads: unrouted; 550 µm: routed, under the 500 default).

## Traps pinned (the #91 obligations)

1. **Named pair rules ride after the general `(clearance ...)`.** `ClearanceMatrix.setDefaultValue` rewrites every pair [i][j] ≥ 1 to the value — a named rule placed before the general clearance in the structure rule scope is clobbered back to it (first injection attempt: the 800 µm class silently read 250 µm, the general default).
2. **The class must be priced, not just named.** An unpriced class resolves through `Math.max(clearanceMatrix.getNo(name), 0)` — class index 0, no clearance at all — and the built-in override opts out because the outline no longer holds the fallback AREA class (leg D: routed with *zero* violations on geometry the default blocks). Naming the class is an obligation swap: the emitter must always carry the pair values.
3. **Price every item class, not just `wire`.** The `wire_edge` pair makes the reader split SMD pins into their own `smd` class (`containsWireClearancePair` → `createDefaultClearanceClasses`); pads left unpaired keep pricing the 250 µm general default against the edge. The override's own template — board_edge vs *all* classes — is the emission shape.
4. **The pair token splits on `_`**: `(clearance v (type A_B))` prices classes A×B; a single-word unquoted name (`(type edge)`) splits to length 1 and is silently dropped. The class *name* stays underscore-free (ADR-0022's prescription, now with the mechanism: it is the pair token that splits, and the boundary's `(clearance_class edge)` reads the bare name).

## Consequences

- ADR-0022's "by construction" is now measurement, not source-reading; #91 implements the emission against the shape above (order, pair set, values-always), with `TestDsnEdgeClearancePrecedence` as the regression pin behind the jar sha256.
- ADR-0020/0022's "slow-tier pin" wording is superseded by the route tier (ADR-0024's split); noted here rather than re-decided.
- The M3 spec (#79) can state the edge number reaches the router by construction — with trap 2 as the invariant the exporter owes.

Reproduce with `uv run pytest tests/test_dsn_freerouting.py::TestDsnEdgeClearancePrecedence -m route` — the tuples are deterministic under the pinned jar; the pad-violation quirk reading is a machine-fact of this jar version, which is exactly what the pin is for.
