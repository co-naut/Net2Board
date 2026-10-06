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

Both ratios come from one run on one machine — self-normalizing, and asserted ≥1,000× by the slow-tier tests (`uv run pytest -m slow`; requires kicad-cli, java, and the pinned Freerouting jar). Dated receipt with full provenance and the linearity receipt: [docs/results/2026-10-05-m2-index-amendment.md](docs/results/2026-10-05-m2-index-amendment.md) (the runs it supersedes: [2026-10-05-m2-placements-fix.md](docs/results/2026-10-05-m2-placements-fix.md), [2026-10-01-m2-order-amendment.md](docs/results/2026-10-01-m2-order-amendment.md), [2026-10-01-m2.md](docs/results/2026-10-01-m2.md)).

| scope | engine | wrapper (kicad-cli per candidate) | ratio |
|---|---|---|---|
| per-candidate (ecc83, micro) | 56.9 µs/candidate | 2.539 s | 44,596× |
| per-session (tiny_tapeout, macro) | 97.66 s for 1,414,784 candidates (typical move 44.4 µs/probe) | 3,866,843 s derived — 2.733168 s × 1,414,784 | 39,597× |

Fixtures: ecc83 (15 comps / 9 nets) and tiny_tapeout (150 comps / 114 nets, 104.5 × 81 mm, 4 Cu). Oracle: kicad-cli 10.0.6; freerouting-2.4.1 (sha256-pinned). Linux x86_64, Python 3.12.12. The macro row's derived baseline prices the counterfactual wrapper engine: real `kicad-cli pcb drc` runs on fully placed boards of the same fixture, times the session's candidate count.
