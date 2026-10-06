"""S18 — slow tier: the kicad-cli oracle cross-check (ADR-0010).

The outermost verifier: both acceptance faces of the ecc83 board — the
clean placement table and the one-move overlap variant rebuilt on top of
it — are exported and handed to the installed ``kicad-cli pcb drc``
(JSON, ``--severity-all``; probed on kicad-cli 10.0.6). Assertions:

- Parse acceptance: the oracle exits 0, captured against the subprocess
  directly — no shell pipe may mask it (T3 pitfall).
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

import json
import re
import shutil
import subprocess

import pytest

from conftest import load_ecc83_spec
from net2board.build import build_board
from net2board.drc import ViolationType, run_drc
from net2board.examples.ecc83_placements import BEAT_OVERLAP_ROW, apply_placements
from net2board.export import export_pcb

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(
        shutil.which("kicad-cli") is None,
        reason="kicad-cli is not installed — the slow tier is a local "
        "sign-off step, run deliberately with `pytest -m slow` (ADR-0010)",
    ),
]

_ORACLE_OVERLAP_TYPE = ViolationType.COURTYARDS_OVERLAP.value
_DRC_COMMAND = ("kicad-cli", "pcb", "drc", "--format", "json", "--severity-all")


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


def oracle_drc(name, board, tmp_path):
    """Export ``board`` and run the installed oracle over the file.

    Exit 0 is asserted against the subprocess itself: plain ``pcb drc``
    reports violations in the JSON while exiting 0, so 0 here means the
    board was parsed and checked (exit 3 = parse failure, pinned in
    TestOracleContract).
    """
    board_path = tmp_path / f"{name}.kicad_pcb"
    board_path.write_text(export_pcb(board))
    report_path = tmp_path / f"{name}.drc.json"
    result = subprocess.run(
        (*_DRC_COMMAND, "-o", str(report_path), str(board_path)),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        f"kicad-cli exited {result.returncode} (3 = parse failure)\n"
        f"stdout:\n{result.stdout}stderr:\n{result.stderr}"
    )
    return json.loads(report_path.read_text())


def oracle_violation_count(report, violation_type):
    """How many oracle violations carry ``violation_type`` — per type only."""
    return sum(
        1 for violation in report["violations"] if violation["type"] == violation_type
    )


_NET_TOKEN = re.compile(r"\[([^]]+)\]")


def ratsnest_net_names(report):
    """Every net name the oracle's ratsnest references.

    kicad-cli 10.0.6 embeds each pad's net in its item description and
    carries no structured net field, so the bracketed token is the only
    naming available (probed in #18). An item without one fails loudly —
    a report-format change must stop the sign-off, not silently pass it.
    """
    names = set()
    for entry in report["unconnected_items"]:
        for item in entry["items"]:
            match = _NET_TOKEN.search(item["description"])
            assert match, (
                f"oracle pad item carries no [net] token: "
                f"{item['description']!r} — check the installed kicad-cli's "
                f"JSON description format"
            )
            names.add(match.group(1))
    return names


def multi_pad_net_names(board):
    """The nets that can form an unconnected pair: two or more pads."""
    return {net.name for net in board.nets if len(net.pins) >= 2}


@pytest.mark.parametrize(
    ("name", "make_board"),
    [("clean", acceptance_board), ("overlapped", overlapped_board)],
    ids=("clean", "overlapped"),
)
class TestBothBoards:
    def test_oracle_accepts_the_export(self, tmp_path, name, make_board):
        """Parse acceptance: exit 0 and a JSON report (ADR-0010, assertion 1)."""
        oracle_drc(name, make_board(), tmp_path)

    def test_ratsnest_is_present(self, tmp_path, name, make_board):
        """M1 routes nothing — every multi-pad net must show as unrouted."""
        report = oracle_drc(name, make_board(), tmp_path)
        assert len(report["unconnected_items"]) > 0

    def test_ratsnest_covers_exactly_the_multi_pad_nets(
        self, tmp_path, name, make_board
    ):
        board = make_board()
        report = oracle_drc(name, board, tmp_path)
        assert ratsnest_net_names(report) == multi_pad_net_names(board)

    def test_outline_is_valid(self, tmp_path, name, make_board):
        report = oracle_drc(name, make_board(), tmp_path)
        assert oracle_violation_count(report, "invalid_outline") == 0

    def test_containment_is_clean_with_no_oracle_analogue(
        self, tmp_path, name, make_board
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
        report = oracle_drc(name, board, tmp_path)
        assert oracle_violation_count(report, "courtyard_outside_outline") == 0


class TestCourtyardsOverlapParity:
    """ADR-0010 assertion 5 — per-type counts, clean 0 = 0, overlapped 1 = 1."""

    def test_clean_board_is_clean_on_both_sides(self, tmp_path):
        board = acceptance_board()
        assert run_drc(board) == ()
        report = oracle_drc("clean", board, tmp_path)
        assert oracle_violation_count(report, _ORACLE_OVERLAP_TYPE) == 0

    def test_overlapped_variant_counts_match_per_type(self, tmp_path):
        board = overlapped_board()
        ours = sum(
            1
            for violation in run_drc(board)
            if violation.type is ViolationType.COURTYARDS_OVERLAP
        )
        report = oracle_drc("overlapped", board, tmp_path)
        theirs = oracle_violation_count(report, _ORACLE_OVERLAP_TYPE)
        assert ours > 0
        assert theirs == ours


class TestOracleContract:
    def test_parse_failure_surfaces_as_exit_3_with_no_report(self, tmp_path):
        """The exit code is the subprocess's own.

        Piping the oracle through a shell would mask exactly this failure
        mode (T3 pitfall): exit 3 means the board never loaded, and no
        JSON report is written for it.
        """
        board_path = tmp_path / "truncated.kicad_pcb"
        board_path.write_text("(kicad_pcb (version 20241229)\n")
        report_path = tmp_path / "truncated.drc.json"
        result = subprocess.run(
            (*_DRC_COMMAND, "-o", str(report_path), str(board_path)),
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 3
        assert not report_path.exists()
