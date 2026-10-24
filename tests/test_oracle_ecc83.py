"""S18 — slow tier: the kicad-cli oracle cross-check (ADR-0010).

The outermost verifier: both acceptance faces of the ecc83 board — the
clean placement table and the one-move overlap variant rebuilt on top of
it — are exported and handed to the installed ``kicad-cli pcb drc``
(JSON, ``--severity-all``; probed under the exporter's
``ORACLE_TARGET``). Assertions:

- Parse acceptance: the oracle's status is ``PARSED``, mapped from the
  subprocess's own exit code — no shell pipe may mask it (T3 pitfall).
- Ratsnest present: ``unconnected_items`` is non-empty (M1 routes
  nothing, so every multi-pad net must appear unrouted).
- Per-net coverage: the ratsnest names exactly the board's multi-pad
  nets. The JSON carries no structured net field — names are read from
  each pad item's description (``"PTH pad 1 [GND] of "``, probed in
  #18) — and a net with fewer than two pads cannot form an unconnected
  pair, so the four single-pad ``unconnected-(Px-Pad1)`` names are
  structurally absent; the equality asserts both directions.
- No ``invalid_outline`` among the oracle's violations.
- ``courtyards_overlap`` parity per type, never per total: the clean
  board gives 0 = 0, the overlapped variant 1 = 1. The oracle emits
  checks M1 does not model (clearance, holes, ``lib_footprint_issues``
  for the fictional ``Net2Board:`` library) — totals are not comparable.

The module auto-skips with a clear message when ``kicad-cli`` is absent
and is deselected by default (``pytest -m slow`` opts in): the tier
stays a local sign-off step — CI gates the fast tier only (ADR-0010).
"""

import re

import pytest

from conftest import load_ecc83_spec
from net2board.build import build_board
from net2board.drc import ViolationType, run_drc
from net2board.examples.ecc83_placements import BEAT_OVERLAP_ROW, apply_placements
from net2board.export import ORACLE_TARGET, export_pcb
from net2board.harness.oracle import (
    OracleStatus,
    oracle_violation_count,
    ratsnest_net_names,
)

pytestmark = [
    pytest.mark.slow,
]

_ORACLE_OVERLAP_TYPE = ViolationType.COURTYARDS_OVERLAP.value


def acceptance_board():
    """The clean acceptance board, from the committed table."""
    return apply_placements(build_board(load_ecc83_spec()))


def overlapped_board():
    """One ``with_placement`` moving R2 onto R1, on top of the clean board.

    The relocation goes through the upsert path — R2 is already placed —
    and lands squarely on its neighbour: exactly one courtyard violation
    by our DRC (pinned fast-tier in test_acceptance_ecc83.py), and the
    oracle must agree per type.
    """
    return acceptance_board().with_placement(*BEAT_OVERLAP_ROW)


def oracle_drc(oracle_report, name, board):
    """Export ``board`` and run the installed oracle over the file.

    Parse acceptance is the adapter's ``PARSED`` status: plain ``pcb drc``
    reports violations in the JSON while exiting 0, so PARSED here means
    the board was parsed and checked (exit 3 = parse failure, pinned in
    TestOracleContract). The conftest ``oracle_report`` factory is the
    one gate (#104)."""
    return oracle_report(export_pcb(board), name)


_PAD_COPPER_TYPES = (
    ViolationType.SHORTING_ITEMS,
    ViolationType.CLEARANCE,
    ViolationType.COPPER_EDGE_CLEARANCE,
)


class TestPadCopperParity:
    """#91's parity receipt on the acceptance board (ADR-0022): shorts
    and clearance parity 0 = 0, and the edge check is the receipt's one
    pinned divergence — KiCad has no EdgeMount concept and reports the
    near-edge pads; we exempt them by stated intent (the board's
    Requirement EdgeMounts, ADR-0018's mechanism extended to pad
    copper). The oracle's 9 is measured under the judged rules (the
    fresh-project 0.5 mm default our export lets it read)."""

    @pytest.fixture(scope="class")
    def report(self, oracle_report):
        return oracle_report(export_pcb(acceptance_board()), "pad-parity")

    def test_shorts_and_clearance_parity(self, report):
        run = run_drc(acceptance_board())
        for check in (ViolationType.SHORTING_ITEMS, ViolationType.CLEARANCE):
            ours = sum(1 for violation in run if violation.type is check)
            assert ours == oracle_violation_count(report, check.value) == 0

    def test_the_edge_divergence_is_the_exemption(self, report):
        ours = sum(
            1
            for violation in run_drc(acceptance_board())
            if violation.type is ViolationType.COPPER_EDGE_CLEARANCE
        )
        assert ours == 0
        assert oracle_violation_count(report, "copper_edge_clearance") == 9


def multi_pad_net_names(board):
    """The nets that can form an unconnected pair: two or more pads."""
    return {net.name for net in board.nets if len(net.pins) >= 2}


@pytest.fixture(scope="module")
def clean_report(oracle_report):
    """The clean board's oracle report — one kicad-cli run per module."""
    return oracle_drc(oracle_report, "clean", acceptance_board())


@pytest.fixture(scope="module")
def overlapped_report(oracle_report):
    """The overlapped variant's oracle report — one kicad-cli run per module."""
    return oracle_drc(oracle_report, "overlapped", overlapped_board())


@pytest.mark.parametrize(
    ("name", "make_board"),
    [("clean", acceptance_board), ("overlapped", overlapped_board)],
    ids=("clean", "overlapped"),
)
class TestBothBoards:
    """ADR-0010's assertions on both acceptance boards, read off the
    module-scoped ``clean_report`` / ``overlapped_report`` fixtures —
    the oracle runs once per board, not once per test, so the module's
    whole oracle bill is three kicad-cli runs (contract probe included).
    The board itself is rebuilt per test where a comparison needs it:
    an in-process ``build_board`` call, no oracle cost."""

    def test_oracle_accepts_the_export(self, request, name, make_board):
        """Parse acceptance: exit 0 and a JSON report (ADR-0010, assertion 1)."""
        request.getfixturevalue(f"{name}_report")

    def test_ratsnest_is_present(self, request, name, make_board):
        """M1 routes nothing — every multi-pad net must show as unrouted."""
        report = request.getfixturevalue(f"{name}_report")
        assert len(report["unconnected_items"]) > 0

    def test_ratsnest_covers_exactly_the_multi_pad_nets(
        self, request, name, make_board
    ):
        board = make_board()
        report = request.getfixturevalue(f"{name}_report")
        assert ratsnest_net_names(report) == multi_pad_net_names(board)

    def test_outline_is_valid(self, request, name, make_board):
        report = request.getfixturevalue(f"{name}_report")
        assert oracle_violation_count(report, "invalid_outline") == 0

    def test_containment_is_clean_with_no_oracle_analogue(
        self, request, name, make_board
    ):
        """ADR-0018: the oracle has no containment check to pair with — ours
        counts 0 on the acceptance board (the EdgeMounts state its
        overhangs), and the oracle never emits the type. Per-type only, no
        parity claim (ADR-0010's assertion-5 posture, extended)."""
        board = make_board()
        ours = sum(
            1
            for violation in run_drc(board)
            if violation.type is ViolationType.COURTYARD_OUTSIDE_OUTLINE
        )
        assert ours == 0
        report = request.getfixturevalue(f"{name}_report")
        assert oracle_violation_count(report, "courtyard_outside_outline") == 0


class TestCourtyardsOverlapParity:
    """ADR-0010 assertion 5 — per-type counts, clean 0 = 0, overlapped 1 = 1."""

    def test_clean_board_is_clean_on_both_sides(self, request):
        board = acceptance_board()
        assert run_drc(board) == ()
        report = request.getfixturevalue("clean_report")
        assert oracle_violation_count(report, _ORACLE_OVERLAP_TYPE) == 0

    def test_overlapped_variant_counts_match_per_type(self, request):
        board = overlapped_board()
        ours = sum(
            1
            for violation in run_drc(board)
            if violation.type is ViolationType.COURTYARDS_OVERLAP
        )
        report = request.getfixturevalue("overlapped_report")
        theirs = oracle_violation_count(report, _ORACLE_OVERLAP_TYPE)
        assert ours > 0
        assert theirs == ours


class TestOracleContract:
    def test_installed_oracle_meets_the_declared_target(self, kicad_cli):
        """The tier's parity claims are probed facts bound to the
        exporter's declared oracle floor (``ORACLE_TARGET``, #86): an
        installed kicad-cli older than the floor fails here loudly,
        instead of silently re-checking those claims against an
        unsupported version. The floor is parsed from the declaration
        itself — one source, never a second copy of the version here.
        """
        version = kicad_cli.version()
        assert version != "absent", (
            "kicad-cli is absent or broken — the slow tier is a local "
            "sign-off step run deliberately with `pytest -m slow`"
        )
        installed = tuple(int(part) for part in version.split("."))
        floor_match = re.search(r">=\s*(\d+(?:\.\d+){,2})", ORACLE_TARGET)
        assert floor_match, f"ORACLE_TARGET {ORACLE_TARGET!r} carries no floor"
        floor = tuple(int(part) for part in floor_match.group(1).split("."))
        assert installed >= floor, (
            f"installed kicad-cli {version} predates the declared "
            f"{ORACLE_TARGET} — this tier's probes were verified against the "
            f"floor, never against an older oracle; re-probe on the installed "
            f"version, then move ORACLE_TARGET"
        )

    def test_parse_failure_surfaces_as_exit_3_with_no_report(self, kicad_cli, tmp_path):
        """The status is the subprocess's own exit code, mapped in the
        adapter (exit 3 → ``PARSE_FAILED``).

        Piping the oracle through a shell would mask exactly this failure
        mode (T3 pitfall): exit 3 means the board never loaded, and no
        JSON report is written for it.
        """
        outcome = kicad_cli.drc_report(
            "(kicad_pcb (version 20241229)\n", "truncated", tmp_path
        )
        assert outcome.status is OracleStatus.PARSE_FAILED
        assert outcome.report is None
        assert not (tmp_path / "truncated.drc.json").exists()
