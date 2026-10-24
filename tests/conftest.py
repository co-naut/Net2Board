"""Shared test world: hand-built IR plus the committed ecc83 fixture paths.

The world of ``TWO_RESISTORS`` (deliberately not a real circuit) is the
library-embedder path — boards from IR, no fixture files:

- comps ``R1``, ``R2`` both use entry ``RES``; ``R3`` uses entry ``BIGRES`` —
  three comps so join iteration is observable, two sharing a footprint so
  footprint IR reuse is exercised.
- net ``GND`` joins (R1, pin 2) and (R2, pin 2); net ``N1`` holds (R1, pin 1)
  alone — every net is unrouted in M1, single-pin nets included.
- entry ``RES`` carries an *extra* pad ``"9"`` with no netlist pin anywhere —
  the extra-pad case stays silent by contract (ADR-0008).
- entry ``BIGRES`` carries a circle courtyard whose defining end point is
  off-axis: radius² = 10 × 10¹² nm² but radius = √10 × 10⁶ nm — not an
  integer nm — exercising the exact-integer representation; ``RES`` carries
  a rectangle courtyard.
- outline is a 40 mm x 30 mm board in integer nm.

``ECC83_NETLIST`` / ``ECC83_FP_DIR`` / ``load_ecc83_spec()`` serve the
file-driven loader tests over the committed ecc83 fixture;
``TINY_NETLIST`` / ``TINY_FP_DIR`` / ``load_tiny_spec()`` do the same over
the tiny_tapeout hard-placement fixture (#30) — 150 comps, 4 copper
layers, SMD-majority pads.
"""

import io
import json
import os
import re
import shutil
from pathlib import Path
from time import perf_counter

import pytest

from net2board.boardspec import BoardSpec
from net2board.build import build_board
from net2board.geometry import Circle, Point, Rectangle
from net2board.harness.oracle import KICAD_CLI, OracleStatus
from net2board.harness.paths import SessionPaths
from net2board.harness.session import measure_acceptance, run_tiny_session
from net2board.ir import CompIR, FootprintIR, NetIR, NetlistIR, PadIR

# The fixture roots, stated once: the harness's SessionPaths is the one
# resolution site (#68, #104) — conftest derives its names from it.
_SESSION_PATHS = SessionPaths.repo_default()
ECC83_DIR = _SESSION_PATHS.ecc83_dir
ECC83_NETLIST = _SESSION_PATHS.ecc83_netlist
ECC83_FP_DIR = _SESSION_PATHS.ecc83_footprints
TINY_DIR = _SESSION_PATHS.tiny_dir
TINY_NETLIST = _SESSION_PATHS.tiny_netlist
TINY_FP_DIR = _SESSION_PATHS.tiny_footprints

RES_PADS = (
    PadIR(
        number="1",
        local_pos=Point(0, 0),
        size=(1_600_000, 1_600_000),
        drill=800_000,
        layers=("*.Cu", "*.Mask"),
        shape_enum="circle",
    ),
    PadIR(
        number="2",
        local_pos=Point(7_620_000, 0),
        size=(1_600_000, 1_600_000),
        drill=800_000,
        layers=("*.Cu", "*.Mask"),
        shape_enum="oval",
    ),
    PadIR(
        number="9",
        local_pos=Point(-1_000_000, 2_000_000),
        size=(900_000, 900_000),
        drill=500_000,
        layers=("*.Cu", "*.Mask"),
        shape_enum="circle",
    ),
)
BIGRES_PADS = (
    PadIR(
        number="1",
        local_pos=Point(0, 0),
        size=(2_000_000, 2_000_000),
        drill=1_000_000,
        layers=("*.Cu", "*.Mask"),
        shape_enum="rect",
    ),
    PadIR(
        number="2",
        local_pos=Point(10_000_000, 0),
        size=(2_000_000, 2_000_000),
        drill=1_000_000,
        layers=("*.Cu", "*.Mask"),
        shape_enum="circle",
    ),
)

RES_COURTYARD = Rectangle(
    min=Point(-1_050_000, -1_500_000), max=Point(8_670_000, 1_500_000)
)
BIGRES_COURTYARD = Circle(
    center=Point(5_000_000, 0),
    radius_sq=10_000_000_000_000,
    end=Point(8_000_000, 1_000_000),
)

OUTLINE = Rectangle(min=Point(0, 0), max=Point(40_000_000, 30_000_000))
STACKUP = ("F.Cu", "B.Cu")

FOOTPRINT_IRS = {
    "RES": FootprintIR(entry_name="RES", pads=RES_PADS, courtyard=RES_COURTYARD),
    "BIGRES": FootprintIR(
        entry_name="BIGRES", pads=BIGRES_PADS, courtyard=BIGRES_COURTYARD
    ),
}

NETLIST_IR = NetlistIR(
    comps=(
        CompIR(ref="R1", entry_name="RES"),
        CompIR(ref="R2", entry_name="RES"),
        CompIR(ref="R3", entry_name="BIGRES"),
    ),
    nets=(
        NetIR(name="GND", nodes=(("R1", "2"), ("R2", "2"))),
        NetIR(name="N1", nodes=(("R1", "1"),)),
    ),
)


def make_spec(
    netlist_ir=NETLIST_IR, footprint_irs=FOOTPRINT_IRS, design_rules=None
) -> BoardSpec:
    """A BoardSpec over the shared hand-built IR (overridable per test)."""
    from net2board.model import DesignRules

    return BoardSpec(
        netlist_ir=netlist_ir,
        footprint_irs=footprint_irs,
        outline=OUTLINE,
        stackup=STACKUP,
        design_rules=design_rules or DesignRules(),
    )


def counted_probes(board, budget: int, refs: tuple[str, ...] | None = None) -> int:
    """solve at ``budget`` with a counting wrapper on the Evaluator's
    counted verdict — the candidates-evaluated ledger (one candidate =
    one ``probe_key`` read, ADR-0016 as amended by #64). Patches the seam
    the search actually reads: a wrapper on any other method counts zero
    forever, and ``probes <= budget`` passes vacuously. Optional ``refs``
    scopes the solve the way ``solve``'s ``refs`` does, so a bucket's or
    named row's own spend is ledger-countable. The tests' trick —
    instrumentation is never the harness's way (#102's seam change
    retires it)."""
    import net2board.solver._solve as solve_module
    from net2board.solver import solve

    real_probe_key = solve_module.Evaluator.probe_key
    count = 0

    def counting_probe_key(self, ref, pose):
        nonlocal count
        count += 1
        return real_probe_key(self, ref, pose)

    solve_module.Evaluator.probe_key = counting_probe_key
    try:
        solve(board, refs, budget=budget)
    finally:
        solve_module.Evaluator.probe_key = real_probe_key
    return count


# The rotation tests' shared world (#90): SWIVEL is PAIRED's pad
# geometry — two unit circles at (0, 0) and (2 mm, 0) — under a yard
# that actually covers the pads, so 4 × 2 mm at rotation 0 and 2 × 4 mm
# at 90°; TINY is the one-pad unit part (±0.5 mm square yard) the files'
# local worlds also carry.
SWIVEL_PADS = (
    PadIR(
        number="1",
        local_pos=Point(0, 0),
        size=(1_000_000, 1_000_000),
        drill=None,
        layers=("F.Cu",),
        shape_enum="circle",
    ),
    PadIR(
        number="2",
        local_pos=Point(2_000_000, 0),
        size=(1_000_000, 1_000_000),
        drill=None,
        layers=("F.Cu",),
        shape_enum="circle",
    ),
)
SWIVEL_YARD = Rectangle(Point(-1_000_000, -1_000_000), Point(3_000_000, 1_000_000))
SWIVEL_WORLD = {
    "TINY": FootprintIR(
        entry_name="TINY",
        pads=(
            PadIR(
                number="1",
                local_pos=Point(0, 0),
                size=(1_000_000, 1_000_000),
                drill=None,
                layers=("F.Cu",),
                shape_enum="circle",
            ),
        ),
        courtyard=Rectangle(Point(-500_000, -500_000), Point(500_000, 500_000)),
    ),
    "SWIVEL": FootprintIR(entry_name="SWIVEL", pads=SWIVEL_PADS, courtyard=SWIVEL_YARD),
}


def fresh_board():
    """The unplaced Board from the shared IR — one build_board call."""
    return build_board(make_spec())


def load_ecc83_spec() -> BoardSpec:
    """The ecc83 fixture loaded through the file-driven path."""
    return BoardSpec.from_files(ECC83_NETLIST, ECC83_FP_DIR, OUTLINE, STACKUP)


TINY_OUTLINE = Rectangle(min=Point(0, 0), max=Point(104_500_000, 81_000_000))
TINY_STACKUP = ("F.Cu", "In1.Cu", "In2.Cu", "B.Cu")


def load_tiny_spec() -> BoardSpec:
    """The tiny_tapeout fixture loaded through the file-driven path."""
    return BoardSpec.from_files(TINY_NETLIST, TINY_FP_DIR, TINY_OUTLINE, TINY_STACKUP)


def assert_golden(dir_name: str, name: str, value, regen_hint: str) -> None:
    """Assert ``value`` renders byte-equal to a sorted-keys JSON golden.

    Enums serialize as their values. Regeneration only happens through the
    deliberate ``UPDATE_GOLDENS=1`` path — hand edits are never legitimate
    (ADR-0010).
    """
    rendered = (
        json.dumps(value, indent=2, sort_keys=True, default=lambda o: o.value) + "\n"
    )
    _check_golden(dir_name, name, rendered, regen_hint)


def assert_byte_golden(dir_name: str, name: str, text: str, regen_hint: str) -> None:
    """Assert ``text`` is byte-equal to a raw golden (export output).

    Same contract as ``assert_golden``: regeneration only through
    ``UPDATE_GOLDENS=1`` — a byte-diff is the cheapest, least-gameable
    regression signal for serialized output (ADR-0010).
    """
    _check_golden(dir_name, name, text, regen_hint)


def _check_golden(dir_name: str, name: str, rendered: str, regen_hint: str) -> None:
    golden_dir = Path(__file__).parent / "goldens" / dir_name
    path = golden_dir / name
    if os.environ.get("UPDATE_GOLDENS") == "1":
        golden_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(rendered)
        return
    assert path.exists(), f"{name} missing — {regen_hint}"
    assert path.read_text() == rendered, f"{name} stale — {regen_hint}"


@pytest.fixture(scope="session")
def tiny_session_record():
    """The tiny_tapeout headline (ADR-0019's slow-tier acceptance), run
    once per slow invocation — a full-budget solve over 150 components."""
    return run_tiny_session()


@pytest.fixture(scope="session")
def kicad_cli():
    """The installed oracle, or a skip — the suite's one kicad-cli gate
    (#104): the skip markers it replaces lived hand-copied in the oracle
    tests and the acceptance fixture."""
    if shutil.which("kicad-cli") is None:
        pytest.skip(
            "kicad-cli is not installed — the slow tier is a local "
            "sign-off step, run deliberately with `pytest -m slow` (ADR-0010)"
        )
    return KICAD_CLI


@pytest.fixture(scope="session")
def oracle_report(kicad_cli, tmp_path_factory):
    """Run the installed oracle over one board's export and return the
    parsed report — the one PARSED gate (#104): the parse-acceptance
    claim is the adapter's ``PARSED`` status (plain ``pcb drc`` reports
    violations in the JSON while exiting 0; exit 3 = parse failure,
    pinned in TestOracleContract), and a failure dumps the subprocess's
    full diagnostics. Workdirs are per board name, session-scoped."""
    workdirs: dict[str, Path] = {}

    def run(board_text: str, name: str) -> dict:
        workdir = workdirs.setdefault(name, tmp_path_factory.mktemp(f"oracle-{name}"))
        outcome = kicad_cli.drc_report(board_text, name, workdir)
        assert outcome.status is OracleStatus.PARSED, (
            f"kicad-cli exited {outcome.returncode} (3 = parse failure)\n"
            f"stdout:\n{outcome.stdout}stderr:\n{outcome.stderr}"
        )
        return outcome.report

    return run


@pytest.fixture(scope="session")
def tiny_acceptance(tmp_path_factory, kicad_cli):
    """The slow-tier measurement face, once per slow invocation — two
    headline solves and the two wrapper loops (minutes of deliberate wall
    time). The wrapper leg is real kicad-cli work, so the oracle's absence
    is a skip, not a failure (the ``kicad_cli`` gate). The perf JSON lands
    in a scratch dir — a pytest run must not write the repo's history
    (#100). The phase log (#101) is collected and asserted here: every
    phase must report, in order, so a silent run can never pass the slow
    tier."""
    progress = io.StringIO()
    report = measure_acceptance(
        results_dir=tmp_path_factory.mktemp("perf-history"), progress=progress
    )
    lines = progress.getvalue().splitlines()
    # The prefix is pinned here too, so "with elapsed wall" holds on the
    # harness path, not only in the PhaseReporter unit tests (#101).
    assert all(re.match(r"^\[\+\d+s\] ", line) for line in lines), lines
    phases = [line.split("] ", 1)[-1] for line in lines]
    assert phases == [
        "acceptance run started",
        "provenance done",
        "tiny session done",
        "replay done",
        "tiny wrapper baseline done",
        "move mix done",
        "typical move done",
        "named movers done",
        "ecc83 session done",
        "ecc83 wrapper baseline done",
        "ADR-0001 re-measures done",
        "perf JSON written",
    ]
    return report


# ---------------------------------------------------------------------------
# Fast-tier ceiling visibility (ADR-0026, docs/agents/profiling.md): the
# ≤60 s ceiling fails nothing — a run over it opens a ticket. This hook puts
# the overrun in the run's own summary instead of waiting for someone to
# notice --durations. #91's 20× per-probe regression sat under an 89 s wall
# for a day because nothing spoke up.

_FAST_TIER_CEILING_S = 60.0
"""The fast tier's wall ceiling on the developer machine — restated from
``docs/agents/profiling.md`` ("read at sign-off", ADR-0026). Reported,
never failed: the escalation is a ticket, not a red run."""

_FAST_TIER_STARTED = pytest.StashKey[float]()
"""``perf_counter`` at session start, stashed on the session itself.
This sat in a module global until #109: the ceiling tests fake walls by
writing that global, and the last write outlived the test into the real
run's ``sessionfinish`` — a fabricated 10 000 s warning on every fast
run. The session owns its start; no other session's test can reach it."""


def pytest_sessionfinish(session: pytest.Session, exitstatus) -> None:
    """The tier read is post-deselection, from ``session.items``:
    reading markers at collection time would see the slow/route items
    the ``-m "not slow and not route"`` addopt is about to deselect and
    mistake every fast run for a gated tier."""
    items = list(getattr(session, "items", ()) or ())
    if not items or any(
        "route" in item.keywords or "slow" in item.keywords for item in items
    ):
        return
    started = session.stash.get(_FAST_TIER_STARTED, None)
    if started is None:
        return
    wall_s = perf_counter() - started
    if wall_s <= _FAST_TIER_CEILING_S:
        return
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    if reporter is None:
        return
    reporter.write_line(
        f"\nWARNING: fast tier wall {wall_s:.1f}s is over the "
        f"{_FAST_TIER_CEILING_S:.0f}s ceiling (docs/agents/profiling.md, "
        f"ADR-0026) — nothing failed, but the overrun opens a ticket "
        f"(/diagnosing-bugs); compare the latest receipt in docs/results/ "
        f"first, and the quick reading is `profile tiny --bench`"
    )


def pytest_sessionstart(session: pytest.Session) -> None:
    session.stash[_FAST_TIER_STARTED] = perf_counter()
