# Net2Board

A Python engine that turns design intent into legal geometry. An external AI orchestrator states the relations — "these decoupling caps within 2 mm of U1's power pins", "the power section in this region of the board" — and Net2Board solves for the exact integer coordinates: budgeted deterministic placement search, exact integer-nanometre geometric checks, and byte-deterministic `.kicad_pcb` and Specctra DSN export that KiCad and Freerouting open natively.

## Ambition

1. **Intent to geometry** — relational constraints in (Requirements and Preferences on a Board), exactly-solved legal placements out
2. **The engine layer beneath AI orchestrators** — the orchestrator states intent and judges outcomes in tens of calls; the engine's inner loop evaluates candidates algorithmically, with no oracle call in the path — a wrapper shelling out to a slow oracle for every candidate cannot follow at any price
3. **Exactness as the contract** — integer nanometres, deterministic results given the arguments, `kicad-cli` as the cross-checking DRC authority (Net2Board is not the sign-off oracle)
4. **Routing delegated, not searched** — a pure DSN handoff to Freerouting; routed copper re-enters the loop as intent, never as engine state

Out of scope: orchestrator skills and PCB-design conventions, simulation (SI/PI/thermal/EM), a second EDA input format, MCP/orchestrator transport, being a KiCad wrapper or an end-user product.

## Development

Requires [uv](https://docs.astral.sh/uv/).

```console
uv sync                     # set up the environment (Python 3.12)
uv run ruff check           # lint
uv run ruff format --check  # format check
uv run pytest               # fast-tier tests
```

## Measured

Both ratios come from one run on one machine — self-normalizing, and asserted ≥1,000× by the slow-tier tests (`uv run pytest -m slow`; requires kicad-cli). The routing beats are a separate opt-in tier (`uv run pytest -m route`; needs java, the sha-pinned Freerouting jar, system pcbnew, and kicad-cli — the human baseline re-measures only under `NET2BOARD_REMEASURE_HUMAN=1`). Dated receipt with full provenance: [docs/results/2026-10-25-m3-signoff.md](docs/results/2026-10-25-m3-signoff.md) (the runs it supersedes: [2026-10-24-m3-pad-checks.md](docs/results/2026-10-24-m3-pad-checks.md) — the pad-checks re-pin — which supersedes [2026-10-13-per-candidate-remeasure.md](docs/results/2026-10-13-per-candidate-remeasure.md) — the linearity receipt — which supersedes [2026-10-05-m2-index-amendment.md](docs/results/2026-10-05-m2-index-amendment.md), which supersedes [2026-10-05-m2-placements-fix.md](docs/results/2026-10-05-m2-placements-fix.md), [2026-10-01-m2-order-amendment.md](docs/results/2026-10-01-m2-order-amendment.md), [2026-10-01-m2.md](docs/results/2026-10-01-m2.md)).

| scope | engine | wrapper (kicad-cli per candidate) | ratio |
|---|---|---|---|
| per-candidate (ecc83, micro) | 98.9 µs/candidate | 2.578 s | 26,075× |
| per-session (tiny_tapeout, macro) | 200.61 s for 1,249,767 candidates (typical move 71.6 µs/probe) | 3,366,460.8 s derived — 2.693671 s × 1,249,767 | 16,781× |

## Routed

The M3 benchmark (ADR-0024): both placements routed under identical rules — `DesignRules()` 0.2/0.5 mm + `RouteSizes()` 0.25/0.8 mm, the same sha-pinned freerouting-2.4.1 jar and arguments. Enforced: the unrouted comparison (solver ≤ human) and zero enforced copper findings before routing, under both judges; after routing, each enforced type reads no worse than the human board's row (here: cleaner on all four). Reported, never enforced: the COMPLETED states, the violation tallies, the walls. Reproduce with `uv run pytest -m "slow or route"` (~50 min with the human gate off). Full provenance: [the dated receipt](docs/results/2026-10-25-m3-signoff.md).

| fixture | placement | state | unrouted | router violations | enforced post-route (shorts/crossings/clearance/edge) | router wall |
|---|---|---|---|---|---|---|
| tiny_tapeout | human (demo placement, pinned) | COMPLETED | 71 | 45 | 3 / 0 / 1 / 2 | pinned |
| tiny_tapeout | solver (knob off) | COMPLETED | 61 | 18 | 0 / 0 / 0 / 0 | ≈32 min (derived) |

Fixtures: ecc83 (15 comps / 9 nets) and tiny_tapeout (150 comps / 114 nets, 104.5 × 81 mm, 4 Cu). Oracle: kicad-cli 10.0.7; freerouting-2.4.1 (sha256-pinned). Linux x86_64, Python 3.12.12. The macro row's derived baseline prices the counterfactual wrapper engine: real `kicad-cli pcb drc` runs on fully placed boards of the same fixture, times the session's candidate count.
