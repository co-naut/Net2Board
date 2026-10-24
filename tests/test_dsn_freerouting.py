"""Slow tier — the Freerouting acceptance loop for ``export_dsn`` (#35).

The full handoff chain on two boards, pinned to freerouting-2.4.1 by
sha256 (ADR-0017): export DSN → route headless → read completion from
the log's ``state:`` and ``(N unrouted and M violations)`` tuple (exit 0
is liveness only, never routedness) → re-import the SES onto our
``.kicad_pcb`` export via ``pcbnew.ImportSpecctraSES`` (system python3 —
the venv cannot import the 3.14-built bindings) → ``kicad-cli pcb drc``
before/after.

Two boards, one chain: the hand-tuned acceptance placement (pinned
expectations, #35) and the M2 session's searched ecc83 board — the
routed round trip ADR-0019 gates relatively (COMPLETED, unrouted no
worse than the hand-tuned 3). The tiny_tapeout human baseline (#73)
completes the set: the demo board's own placement, the yardstick M3's
acceptance grilling measures the solver against, first measured at the
old 0.25 mm rules ((90, 108) — the human placement does not route clean
either) and re-measured under the shipped exporter (#85: (71, 45), the
router shorting 3 of its own) and pinned here. #85 routes the solver's
own tiny_tapeout headline beside it:
knob off is ADR-0024's sign-off face (the three acceptance clauses,
the human counts the yardstick); knob on (``NET2BOARD_ROUTE_KNOB_ON=1``)
is the rotation search's first routed reading — a measurement and a
sizing datum (ADR-0025), never an acceptance.

Two probe-pinned contract details the chain depends on:

- The SES **placement echo is dropped** before import:
  ``ImportSpecctraSES`` re-places components from it and corrupts
  back-side worlds (C1 measured: pads moved 10 mm at rot 0, shorting
  two nets). The echo carries nothing the board doesn't already hold —
  the placement was ours.
- The router's ``(N unrouted …)`` maps onto KiCad's ratsnest: the
  imported copper leaves exactly N unconnected items.

Deselected by default (``pytest -m route`` opts in — ADR-0024's third
tier, beside ``slow``); auto-skips when the jar, java, system
python3+pcbnew, or kicad-cli are absent. A present jar with the wrong
sha256 is a hard failure, not a skip — the pin is the determinism
guarantee (byte-identical SES within one jar version).
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from collections import Counter
from pathlib import Path

import pytest

from conftest import load_ecc83_spec, load_tiny_spec
from net2board.boardspec import BoardSpec
from net2board.build import build_board
from net2board.drc import run_drc
from net2board.examples.ecc83_placements import apply_placements
from net2board.examples.tiny_demo_placements import apply_demo_placements
from net2board.export import export_dsn, export_pcb
from net2board.geometry import Point, Rectangle
from net2board.harness.paths import SessionPaths, freerouting_jar_default
from net2board.harness.session import (
    TINY_OUTLINE,
    TINY_STACKUP,
    run_session,
    run_tiny_session,
)
from net2board.ir import CompIR, FootprintIR, NetIR, NetlistIR, PadIR
from net2board.model import DesignRules
from net2board.objective import evaluate
from net2board.solver import solve

JAR = freerouting_jar_default()
JAR_SHA256 = "251101c3eeac22d7e7dfcf6796603279e5d1000283eb82d8f093780f7afc6aa9"

# Pinned truths under the sha256-pinned jar on the acceptance board.
# The 18 violations are the placement's own copper conflicts (pads over
# mounting-hole copper, edge-adjacent pads) — constant across every
# rule set probed, placement facts rather than writer bugs; the router
# merely reports them, and the DRC leg proves it introduces nothing.
EXPECTED_UNROUTED = 3
EXPECTED_VIOLATIONS = 18

_STATE = re.compile(r"finished with state: (\w+)")
_FINAL_SCORE = re.compile(
    r"final score: [\d.]+ \((\d+) unrouted and (\d+) violations\)"
)

_REIMPORT_SCRIPT = """
import pcbnew

board = pcbnew.LoadBoard({pcb!r})
assert pcbnew.ImportSpecctraSES(board, {ses!r}), "ImportSpecctraSES returned False"
pcbnew.SaveBoard({out!r}, board)
"""


def _pcbnew_python() -> str | None:
    """An interpreter that imports pcbnew — the venv's own python3 (first
    on PATH under ``uv run``) cannot: the bindings are built for the
    system 3.14."""
    candidates = ["/usr/bin/python3", shutil.which("python3") or ""]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            probe = subprocess.run(
                [candidate, "-c", "import pcbnew"],
                capture_output=True,
                text=True,
                check=False,
            )
            if probe.returncode == 0:
                return candidate
    return None


_PCBNEW_PY = _pcbnew_python()


REMEASURE_ENV = "NET2BOARD_REMEASURE_HUMAN"
"""The human baseline's re-measure gate (ADR-0024): the tuple is pinned
as constants and re-measured only when its inputs change — the
``DesignRules`` defaults, the jar pin, or the exporter. Set to ``1`` to
run the ~1 h chain; unset, the chain skips with the triggers named."""

pytestmark = [
    pytest.mark.route,
    pytest.mark.skipif(
        not JAR.exists() or shutil.which("java") is None,
        reason="freerouting jar or java absent — the route tier is a local "
        "sign-off step, run deliberately with `pytest -m route` (ADR-0017, ADR-0024)",
    ),
    pytest.mark.skipif(
        _PCBNEW_PY is None or shutil.which("kicad-cli") is None,
        reason="system python3+pcbnew or kicad-cli absent — the SES re-import "
        "and DRC legs need the installed KiCad toolchain",
    ),
]


def _route_chain(work, dsn_path, pcb_path, timeout_s=300):
    """Route ``dsn_path`` headlessly and re-import the SES onto
    ``pcb_path``, DRC-ing both sides. Everything downstream asserts on
    the returned dict. ``timeout_s`` is the router's wall leash — 300 s
    covers the two ecc83 boards; the tiny human baseline runs longer."""
    digest = hashlib.sha256(JAR.read_bytes()).hexdigest()
    if digest != JAR_SHA256:
        pytest.fail(f"{JAR} sha256 {digest} != pinned {JAR_SHA256}")

    ses_path = work / "board.ses"
    user_data = work / "ud"
    route = subprocess.run(
        [
            "timeout",
            str(timeout_s),
            "java",
            "-jar",
            str(JAR),
            "-de",
            str(dsn_path),
            "-do",
            str(ses_path),
            "--gui.enabled=false",
            "--api_server.enabled=false",
            "-da",
            f"--user_data_path={user_data}",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    log = route.stdout + route.stderr

    if not ses_path.exists():
        pytest.fail(
            f"router produced no SES within the {timeout_s} s leash "
            f"(returncode {route.returncode}) — the leash kill is itself a "
            f"reading; log tail:\n{log[-1500:]}"
        )

    stripped_ses = work / "board_noplace.ses"
    text = ses_path.read_text()
    head = text[: text.index("(placement")]
    tail = text[text.index("(was_is") :]
    stripped_ses.write_text(head + "(placement (resolution um 1)\n  )\n  " + tail)

    routed_path = work / "routed.kicad_pcb"
    reimport = subprocess.run(
        [
            _PCBNEW_PY,
            "-c",
            _REIMPORT_SCRIPT.format(
                pcb=str(pcb_path), ses=str(stripped_ses), out=str(routed_path)
            ),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    reports = {}
    for name, path in (("pre", pcb_path), ("post", routed_path)):
        report_path = work / f"{name}.drc.json"
        subprocess.run(
            (
                "kicad-cli",
                "pcb",
                "drc",
                "--format",
                "json",
                "--severity-all",
                "-o",
                str(report_path),
                str(path),
            ),
            capture_output=True,
            text=True,
            check=False,
        )
        reports[name] = json.loads(report_path.read_text())

    return {
        "returncode": route.returncode,
        "log": log,
        "ses": ses_path.read_text(),
        "reimport": reimport,
        "drc": reports,
    }


@pytest.fixture(scope="module")
def chain(tmp_path_factory):
    """The whole chain on the hand-tuned acceptance board, run once."""
    work = tmp_path_factory.mktemp("dsn-freerouting")
    board = apply_placements(build_board(load_ecc83_spec()))
    dsn_path = work / "board.dsn"
    pcb_path = work / "board.kicad_pcb"
    dsn_path.write_text(export_dsn(board))
    pcb_path.write_text(export_pcb(board))
    return _route_chain(work, dsn_path, pcb_path)


@pytest.fixture(scope="module")
def session_chain(tmp_path_factory):
    """The same chain on the M2 session's searched ecc83 board — the
    round trip ADR-0019 gates relatively."""
    work = tmp_path_factory.mktemp("dsn-session")
    record = run_session()
    dsn_path = work / "session.dsn"
    pcb_path = work / "session.kicad_pcb"
    dsn_path.write_text(export_dsn(record.steps[-1].board))
    pcb_path.write_text(record.export)
    return _route_chain(work, dsn_path, pcb_path)


@pytest.fixture(scope="module")
def human_chain(tmp_path_factory):
    """The chain on the tiny_tapeout human baseline (#73): the demo
    board's own placement on the fixture substrate, the measurement M3's
    acceptance grilling inherits. The router needed ~57 minutes here at
    the 0.25 mm rules (3419 s, #73) and ~29 minutes at the shipped
    rules (1754 s, #85's re-measure), so the leash stays 5400 s —
    ecc83's 300 s covers two boards of a tenth the size.

    Gated by ``NET2BOARD_REMEASURE_HUMAN=1`` (ADR-0024's re-measure
    policy): the pin catches drift at the re-measure, never on every
    run."""
    if os.environ.get(REMEASURE_ENV) != "1":
        pytest.skip(
            "human tuple pinned as constants — set NET2BOARD_REMEASURE_HUMAN=1 "
            "to re-measure (~1 h) after the DesignRules defaults, the jar "
            "pin, or the exporter changed (ADR-0024)"
        )
    work = tmp_path_factory.mktemp("dsn-human-baseline")
    board = apply_demo_placements(build_board(load_tiny_spec()))
    dsn_path = work / "human.dsn"
    pcb_path = work / "human.kicad_pcb"
    dsn_path.write_text(export_dsn(board))
    pcb_path.write_text(export_pcb(board))
    return _route_chain(work, dsn_path, pcb_path, timeout_s=5400)


class TestRoutingLeg:
    def test_exit_zero_is_liveness_and_the_state_says_completed(self, chain):
        assert chain["returncode"] == 0
        states = _STATE.findall(chain["log"])
        assert states and states[-1] == "COMPLETED"

    def test_final_score_tuple_is_pinned(self, chain):
        scores = _FINAL_SCORE.findall(chain["log"])
        assert scores, "no (N unrouted and M violations) tuple in the log"
        unrouted, violations = map(int, scores[-1])
        assert unrouted == EXPECTED_UNROUTED
        assert violations == EXPECTED_VIOLATIONS

    def test_ses_exists_and_carries_routes(self, chain):
        assert "(routes" in chain["ses"]
        assert "(wire" in chain["ses"]


class TestReimportLeg:
    def test_import_silently_skipped_nothing(self, chain):
        """The lying boolean: a partial import still returns True — the
        wx log's 'skipped' warning is the only truth (#22)."""
        assert chain["reimport"].returncode == 0, chain["reimport"].stderr[-2000:]
        output = (chain["reimport"].stdout + chain["reimport"].stderr).lower()
        assert "skipped" not in output

    def test_ratsnest_matches_the_routers_unrouted(self, chain):
        scores = _FINAL_SCORE.findall(chain["log"])
        unrouted = int(scores[-1][0])
        before = len(chain["drc"]["pre"]["unconnected_items"])
        after = len(chain["drc"]["post"]["unconnected_items"])
        assert before > after, "routing changed nothing the oracle can see"
        assert after == unrouted

    def test_no_new_violations_of_any_type(self, chain):
        """Per-type counts may not grow and no type may appear — the
        strongest reading of 'no new copper/clearance violations' (the
        pre-existing pad conflicts stay, identical in count)."""
        pre = Counter(v["type"] for v in chain["drc"]["pre"]["violations"])
        post = Counter(v["type"] for v in chain["drc"]["post"]["violations"])
        delta = post - pre
        assert not delta, f"re-import introduced violations: {dict(delta)}"


class TestSearchedSessionRoundTrip:
    """ADR-0019's gating round trip, on the session's searched ecc83
    board — judged relatively: COMPLETED, and no worse than the
    hand-tuned placement's pinned 3 unrouted. The violations count is
    the placement's own copper conflict report — read, not gated."""

    def test_exit_zero_is_liveness_and_the_state_says_completed(self, session_chain):
        assert session_chain["returncode"] == 0
        states = _STATE.findall(session_chain["log"])
        assert states and states[-1] == "COMPLETED"

    def test_no_worse_than_the_hand_tuned_placement(self, session_chain):
        scores = _FINAL_SCORE.findall(session_chain["log"])
        assert scores, "no (N unrouted and M violations) tuple in the log"
        unrouted, _violations = map(int, scores[-1])
        assert unrouted <= EXPECTED_UNROUTED

    def test_import_silently_skipped_nothing(self, session_chain):
        assert session_chain["reimport"].returncode == 0, session_chain[
            "reimport"
        ].stderr[-2000:]
        output = (
            session_chain["reimport"].stdout + session_chain["reimport"].stderr
        ).lower()
        assert "skipped" not in output

    def test_ratsnest_matches_the_routers_unrouted(self, session_chain):
        scores = _FINAL_SCORE.findall(session_chain["log"])
        unrouted = int(scores[-1][0])
        before = len(session_chain["drc"]["pre"]["unconnected_items"])
        after = len(session_chain["drc"]["post"]["unconnected_items"])
        assert before > after, "routing changed nothing the oracle can see"
        assert after == unrouted

    def test_no_new_violations_of_any_type(self, session_chain):
        pre = Counter(v["type"] for v in session_chain["drc"]["pre"]["violations"])
        post = Counter(v["type"] for v in session_chain["drc"]["post"]["violations"])
        delta = post - pre
        assert not delta, f"re-import introduced violations: {dict(delta)}"


ENFORCED_TYPES = (
    "shorting_items",
    "tracks_crossing",
    "clearance",
    "copper_edge_clearance",
)
"""ADR-0024's enforced KiCad types — clause 2's pre-route zero and
clause 3's post-route no-worse-than population. ``tracks_crossing``
exists kicad-cli side only: routed copper never enters the aggregate
(ADR-0017), so ``run_drc`` carries the three pad-copper types alone."""

HUMAN_UNROUTED = 71
HUMAN_VIOLATIONS = 45
HUMAN_RATSNEST_UNCONNECTED = 71

HUMAN_POST_ENFORCED = {
    "shorting_items": 3,
    "tracks_crossing": 0,
    "clearance": 1,
    "copper_edge_clearance": 2,
}
"""The routed human board's per-enforced-type kicad-cli counts — clause
3's yardstick the solver board is judged no-worse-than. Measured
2026-10-24 (#85's re-measure) under the shipped exporter — 0.2 mm
clearance plus the named 500 um edge class — where the router finishes
notably better than #73's 0.25 mm reading ((71, 45) against (90, 108))
but shorts 3 nets of its own: measured, not smoothed."""


class TestHumanBaselineRoundTrip:
    """#73 — the tiny_tapeout human baseline, the yardstick M3's
    acceptance measures the solver against. First-routed at the old
    0.25 mm rules ((90, 108) — the human placement does not route clean
    either), re-measured 2026-10-24 (#85) under the shipped exporter —
    ``DesignRules`` 0.2/0.5 mm plus the named 500 um edge class
    (ADR-0020/0022) — and re-pinned: COMPLETED with 71 unrouted and 45
    violations. The narrower clearance routes better (71 against 90)
    but the router shorts 3 nets it didn't at 0.25 mm, and narrows
    tracks to 98 ``track_width`` findings (report-only, ADR-0024).
    Two tiny-specific readings, pinned as measured rather than
    smoothed: the unrouted tuple maps onto the ratsnest exactly here
    (71/71), while the solver board below reads 2 slack — the mapping
    is a reading, not a law — and no-new-violation-types does not hold
    (the router's own shorts, clearance, and danglings). The chain runs
    only under ``NET2BOARD_REMEASURE_HUMAN=1`` (ADR-0024's re-measure
    policy)."""

    def test_exit_zero_is_liveness_and_the_state_says_completed(self, human_chain):
        assert human_chain["returncode"] == 0
        states = _STATE.findall(human_chain["log"])
        assert states and states[-1] == "COMPLETED"

    def test_final_score_tuple_is_pinned(self, human_chain):
        scores = _FINAL_SCORE.findall(human_chain["log"])
        assert scores, "no (N unrouted and M violations) tuple in the log"
        unrouted, violations = map(int, scores[-1])
        assert (unrouted, violations) == (HUMAN_UNROUTED, HUMAN_VIOLATIONS)

    def test_the_placement_itself_shorts_nothing(self, human_chain):
        """The pad-fidelity pin: the exported human placement renders
        KiCad-clean of shorts and clearance hits — the first measurement
        run before the #73 exporter fix read 42 phantom shorts (pads
        rendered unrotated on rotated placements); zero is the honest
        board."""
        pre = Counter(v["type"] for v in human_chain["drc"]["pre"]["violations"])
        assert pre["shorting_items"] == 0
        assert pre["clearance"] == 0

    def test_the_router_introduces_no_shorts(self, human_chain):
        """The router's own shorting trade, pinned as measured: #73's
        chain at 0.25 mm introduced none, but the #85 re-measure under
        the shipped rules reads 3 — the yardstick value, not a zero
        claim. Clause 3 judges the solver against exactly this."""
        post = Counter(v["type"] for v in human_chain["drc"]["post"]["violations"])
        assert post["shorting_items"] == HUMAN_POST_ENFORCED["shorting_items"]

    def test_ratsnest_after_routing_is_pinned(self, human_chain):
        after = len(human_chain["drc"]["post"]["unconnected_items"])
        assert after == HUMAN_RATSNEST_UNCONNECTED

    def test_post_route_enforced_types_are_pinned(self, human_chain):
        """Clause 3's yardstick, pinned per type (ADR-0024): the routed
        human board's enforced-type counts, measured at #85's re-measure
        under the shipped exporter — #73's 0.25 mm reading is not the
        yardstick."""
        post = Counter(v["type"] for v in human_chain["drc"]["post"]["violations"])
        assert {
            violation_type: post[violation_type] for violation_type in ENFORCED_TYPES
        } == HUMAN_POST_ENFORCED

    def test_routing_moves_no_placement_class(self, human_chain):
        """The placement's own standing classes — edge-adjacent pads
        (2, the demo's own facts, identical upstream) and courtyard
        overlaps (16, the bounding-hull rendering, upstream's true
        polygons read 8) — routing neither adds nor removes."""
        for violation_type in ("copper_edge_clearance", "courtyards_overlap"):
            pre = Counter(v["type"] for v in human_chain["drc"]["pre"]["violations"])[
                violation_type
            ]
            post = Counter(v["type"] for v in human_chain["drc"]["post"]["violations"])[
                violation_type
            ]
            assert pre == post


# --- the solver's own tiny_tapeout placement, first-routed (#85) -----------
#
# ADR-0023's trigger (a) is a fact nobody has measured: the solver's
# tiny_tapeout headline has never been routed. Two runs, one harness:
#
# - knob off — the sign-off face (ADR-0024): ADR-0019's slow-tier tiny
#   session as shipped (wirelength + containment, no authored intent),
#   routed under the same jar, the same arguments, and the same shipped
#   exporter as the human row. Clause 1 (unrouted no more than the
#   human's) and clause 2 (zero enforced copper findings before routing,
#   ours and kicad-cli per type) are enforced; clause 3 (no worse than
#   the routed human board per enforced type after routing) is enforced
#   against HUMAN_POST_ENFORCED. State, the router's violation tally,
#   and the ratsnest are reported, never enforced. Nothing here adopts a
#   term: worse-than-baseline opens ADR-0023's routability question
#   (placement-caused findings checked first, before the objective is
#   blamed); at-or-better closes the congestion question by measurement.
# - knob on — the rotation search's first routed reading (ADR-0021,
#   ADR-0025), gated by ``NET2BOARD_ROUTE_KNOB_ON=1``: a measurement and
#   a sizing datum, never an acceptance. The solve runs directly against
#   ``solve`` (not the harness's pinned ``_step`` face) under a one-off
#   generous ceiling, so a ``BUDGET_EXHAUSTED`` stop reads as the sizing
#   finding ADR-0025 pre-registered (≈3–4M candidates) rather than a
#   truncated board; ``BUDGET_TINY`` stays pinned for the knob-off
#   headline.

KNOB_ON_ENV = "NET2BOARD_ROUTE_KNOB_ON"
"""The knob-on run's gate (ADR-0024's re-measure posture): the rotation
search's first routed reading is a measurement taken deliberately, not
part of the sign-off face. Set to ``1`` to run it (~1½ h: the solve,
then the route)."""

KNOB_ON_CEILING = 10_000_000
"""The knob-on solve's one-off budget ceiling — generous past ADR-0025's
≈3–4M pre-registered candidate reading, so the stop reason is the
reading, not the leash. Never a ``BUDGET_TINY`` re-pin by itself."""

KNOB_ON_LEASH = 10_800
"""The knob-on route's wall leash — 3 h. The human-sized 5400 s leash
was measured insufficient on 2026-10-24 (the first knob-on route
outlived it and was killed before writing the SES), which is itself a
finding: the board a rotation chose is a materially harder route than
the knob-off board's ~30 min."""


def _dump_reading(work, name, reading):
    """One run's reported readings, written beside its chain artifacts —
    the receipt reads this JSON instead of parsing pytest internals
    (ADR-0024's snapshot provenance)."""
    (work / f"{name}_reading.json").write_text(
        json.dumps(reading, indent=2, sort_keys=True)
    )


@pytest.fixture(scope="module")
def solver_chain(tmp_path_factory):
    """#85 run 1 — the solver's tiny_tapeout headline, knob off. The
    session is ADR-0019's slow-tier acceptance path as shipped
    (~4 min at the current bench); the router needs the human-sized
    leash (5400 s — the human board's measured 57 min is the scale).
    The solve's reported readings ride the fixture; the acceptance
    clauses assert on the chain."""
    record = run_tiny_session()
    work = tmp_path_factory.mktemp("dsn-solver-tiny")
    dsn_path = work / "solver.dsn"
    pcb_path = work / "solver.kicad_pcb"
    dsn_path.write_text(export_dsn(record.headline.board))
    pcb_path.write_text(export_pcb(record.headline.board))
    chain = _route_chain(work, dsn_path, pcb_path, timeout_s=5400)

    scores = _FINAL_SCORE.findall(chain["log"])
    assert scores, "no (N unrouted and M violations) tuple in the log"
    unrouted, violations = map(int, scores[-1])
    states = _STATE.findall(chain["log"])
    pre = Counter(v["type"] for v in chain["drc"]["pre"]["violations"])
    post = Counter(v["type"] for v in chain["drc"]["post"]["violations"])
    ours = Counter(v.type.value for v in run_drc(record.headline.board))
    reading = {
        "objective_nm": record.headline.objective.total_nm,
        "candidates_evaluated": record.headline.candidates_evaluated,
        "stop_reason": record.headline.stop_reason.value,
        "state": states[-1] if states else "?",
        "unrouted": unrouted,
        "violations": violations,
        "ratsnest_unconnected": len(chain["drc"]["post"]["unconnected_items"]),
        "kicad_cli_pre_types": dict(pre),
        "kicad_cli_post_types": dict(post),
        "run_drc_types": dict(ours),
    }
    _dump_reading(work, "solver", reading)
    return {"chain": chain, "work": work, **reading}


class TestSolverTinyRoundTrip:
    """#85 run 1 — ADR-0024's acceptance clauses on the solver's own
    placement, routed under identical rules (the same ``DesignRules``
    defaults, the same ``RouteSizes``, the same jar, the same arguments
    as the human row). Clause 1's scalar is the router's unrouted count
    against ``HUMAN_UNROUTED``; clause 2's zero is per enforced type,
    under ``run_drc`` and kicad-cli both; clause 3 is the post-route
    per-type counts against the routed human board's. No tolerance
    margin: Freerouting is deterministic on these fixtures, a tuple that
    moves on unchanged inputs is a determinism finding that blocks the
    pin."""

    def test_exit_zero_is_liveness_and_the_state_says_completed(self, solver_chain):
        assert solver_chain["chain"]["returncode"] == 0
        assert solver_chain["state"] == "COMPLETED"

    def test_clause_1_no_more_unrouted_than_the_human_baseline(self, solver_chain):
        """ADR-0024 clause 1, the acceptance scalar: the router's own
        unrouted count, same jar, same arguments, at or under the human
        baseline's."""
        assert solver_chain["unrouted"] <= HUMAN_UNROUTED

    def test_clause_2_zero_enforced_findings_under_our_checker(self, solver_chain):
        """ADR-0024 clause 2, ours: the placement's copper legality is
        asserted by construction (legalize reached the legal set, pad
        findings are ERROR — a solver board carrying one is a bug), so
        zero is the honest reading."""
        for violation_type in ("shorting_items", "clearance", "copper_edge_clearance"):
            assert solver_chain["run_drc_types"].get(violation_type, 0) == 0, (
                f"run_drc found {violation_type} on the solver placement"
            )

    def test_clause_2_zero_enforced_findings_in_kicad_before_routing(
        self, solver_chain
    ):
        """ADR-0024 clause 2, the oracle: kicad-cli per enforced type on
        the placement alone — the board the router never sees copper
        on."""
        for violation_type in ENFORCED_TYPES:
            assert solver_chain["kicad_cli_pre_types"].get(violation_type, 0) == 0, (
                f"kicad-cli found {violation_type} on the solver placement"
            )

    def test_clause_3_no_worse_than_the_routed_human_board(self, solver_chain):
        """ADR-0024 clause 3, relative per enforced type: a router
        creates findings no placement controls (the human board's own
        post-route ``clearance``), so post-route cleanliness is judged
        against ``HUMAN_POST_ENFORCED``, never zero."""
        post = Counter(
            v["type"] for v in solver_chain["chain"]["drc"]["post"]["violations"]
        )
        for violation_type, human_count in HUMAN_POST_ENFORCED.items():
            assert post[violation_type] <= human_count, (
                f"post-route {violation_type}: {post[violation_type]} "
                f"> human {human_count}"
            )

    def test_import_silently_skipped_nothing(self, solver_chain):
        assert solver_chain["chain"]["reimport"].returncode == 0, solver_chain["chain"][
            "reimport"
        ].stderr[-2000:]
        output = (
            solver_chain["chain"]["reimport"].stdout
            + solver_chain["chain"]["reimport"].stderr
        ).lower()
        assert "skipped" not in output


@pytest.fixture(scope="module")
def knob_on_chain(tmp_path_factory):
    """#85 run 2 — the same chain with ``free_rotation=True`` (ADR-0021,
    ADR-0025): the first routed board a rotation chose. The solve runs
    directly against ``solve`` from the session's entry board — the
    harness's ``_step`` face pins the knob-off stop reason, and this
    run's stop reason is the reading. Every reported number lands in
    ``knob_on_reading.json`` beside the chain artifacts."""
    if os.environ.get(KNOB_ON_ENV) != "1":
        pytest.skip(
            "the knob-on reading is a deliberate measurement — set "
            f"{KNOB_ON_ENV}=1 to run it (the solve ~25 min, then the route "
            "under a 3 h leash)"
        )
    fixtures = SessionPaths.repo_default()
    entry = build_board(
        BoardSpec.from_files(
            fixtures.tiny_netlist, fixtures.tiny_footprints, TINY_OUTLINE, TINY_STACKUP
        )
    )
    solve_start = time.monotonic()
    result = solve(entry, budget=KNOB_ON_CEILING, free_rotation=True)
    solve_wall_s = time.monotonic() - solve_start
    unplaced = result.board.unplaced_refs()

    work = tmp_path_factory.mktemp("dsn-solver-knob-on")
    # The solve's readings land on disk before the route leg starts — a
    # leash kill (measured 2026-10-24: the first knob-on route outlived
    # the human-sized 5400 s leash) must not eat the measurement.
    _dump_reading(
        work,
        "knob_on_solve",
        {
            "budget_ceiling": KNOB_ON_CEILING,
            "candidates_evaluated": result.candidates_evaluated,
            "stop_reason": result.stop_reason.value,
            "solve_wall_s": round(solve_wall_s, 1),
            "unplaced": list(unplaced),
            "objective_nm": evaluate(result.board).total_nm,
        },
    )

    dsn_path = work / "solver_knob_on.dsn"
    pcb_path = work / "solver_knob_on.kicad_pcb"
    dsn_path.write_text(export_dsn(result.board))
    pcb_path.write_text(export_pcb(result.board))
    chain = _route_chain(work, dsn_path, pcb_path, timeout_s=KNOB_ON_LEASH)

    scores = _FINAL_SCORE.findall(chain["log"])
    assert scores, "no (N unrouted and M violations) tuple in the log"
    unrouted, violations = map(int, scores[-1])
    states = _STATE.findall(chain["log"])
    pre = Counter(v["type"] for v in chain["drc"]["pre"]["violations"])
    post = Counter(v["type"] for v in chain["drc"]["post"]["violations"])
    reading = {
        "budget_ceiling": KNOB_ON_CEILING,
        "candidates_evaluated": result.candidates_evaluated,
        "stop_reason": result.stop_reason.value,
        "solve_wall_s": round(solve_wall_s, 1),
        "unplaced": list(unplaced),
        "objective_nm": evaluate(result.board).total_nm,
        "state": states[-1] if states else "?",
        "unrouted": unrouted,
        "violations": violations,
        "ratsnest_unconnected": len(chain["drc"]["post"]["unconnected_items"]),
        "kicad_cli_pre_types": dict(pre),
        "kicad_cli_post_types": dict(post),
    }
    _dump_reading(work, "knob_on", reading)
    return {"chain": chain, "work": work, "unplaced_refs": unplaced, **reading}


class TestSolverKnobOnReading:
    """#85 run 2 — the rotation knob's first routed reading. A
    measurement, not an acceptance (ADR-0024 reports it beside run 1;
    nothing here adopts a term): the tests assert chain health and the
    strand reading only. The candidate count, its stop reason, and the
    tuple are the receipt's — ADR-0025's ≈3–4M pre-registration and the
    ``BUDGET_TINY`` sizing question settle against the dumped JSON."""

    def test_exit_zero_is_liveness_and_the_state_says_completed(self, knob_on_chain):
        assert knob_on_chain["chain"]["returncode"] == 0
        assert knob_on_chain["state"] == "COMPLETED"

    def test_the_rotation_search_strands_no_part(self, knob_on_chain):
        """The knob-off headline's strand reading is pinned empty; the
        knob-on search sees a strictly larger candidate space, so an
        unplaced ref here is the finding the receipt names."""
        assert knob_on_chain["unplaced_refs"] == ()

    def test_import_silently_skipped_nothing(self, knob_on_chain):
        assert knob_on_chain["chain"]["reimport"].returncode == 0, knob_on_chain[
            "chain"
        ]["reimport"].stderr[-2000:]
        output = (
            knob_on_chain["chain"]["reimport"].stdout
            + knob_on_chain["chain"]["reimport"].stderr
        ).lower()
        assert "skipped" not in output


# --- the DSN edge-clearance precedence pin (#84, ADR-0020/0022) ------------
#
# ADR-0022 makes the checker's edge number the router's by writing
# ``design_rules.edge_clearance_nm`` as an outline ``(clearance_class
# NAME)`` plus named ``<item>_<name>`` pair rules — on the probed
# reading of freerouting-2.4.1's source (``applyCopperToEdgeClearance-
# Override`` keeps an explicit DSN outline class and otherwise forces
# its built-in 500 um). That reading is pinned here against the jar,
# both directions, on throwaway geometry whose routability flips on
# the edge number alone: a 2-pad net across a strip so short no trace
# channel exists once the edge keepout closes the band.
#
# Two reader traps the emission (#91) must respect, both pinned here:
# the named pair rules must come *after* the general ``(clearance
# ...)`` — ``ClearanceMatrix.setDefaultValue`` rewrites every pair,
# clobbering anything set before it — and the class must be priced at
# all: a named-but-unpriced class resolves to ``max(getNo, 0)`` = 0,
# the outline loses its clearance entirely, and the built-in 500 um
# override opts out (leg D: routed with *zero* violations where the
# default blocks). Naming the class is an obligation swap, not a win.

EDGE_PAD = PadIR(
    number="1",
    local_pos=Point(0, 0),
    size=(500_000, 500_000),
    drill=None,
    layers=("F.Cu", "F.Mask"),
    shape_enum="rect",
)


def _edge_probe_board(
    pad_centre_y_nm: int, height_nm: int, edge_clearance_nm: int | None = None
) -> str:
    """A 2-pad, 1-net board whose routability flips on the edge number.

    Two 0.5 mm SMD rects hang ``pad_centre_y_nm - 250 um`` off the
    bottom edge of a 20 mm-wide strip ``height_nm`` tall. With the edge
    requirement at 500 um the horizontal trace band is empty (centre
    would have to lie in [625 um, height - 625 um] — impossible at
    1.2 mm) and the net cannot route; at 300 um the straight shot is
    legal; at 800 um the 1.6 mm board blocks where the default passed.
    The DSN is the production export — the class and its pair values
    ride ``board.design_rules`` (#91); ``edge_clearance_nm`` states the
    board's number, defaulting to the 500 um fresh-project default.
    """
    fp = FootprintIR(entry_name="EDGEPAD", pads=(EDGE_PAD,), courtyard=None)
    spec = BoardSpec(
        netlist_ir=NetlistIR(
            comps=(
                CompIR(ref="P1", entry_name="EDGEPAD"),
                CompIR(ref="P2", entry_name="EDGEPAD"),
            ),
            nets=(NetIR(name="E1", nodes=(("P1", "1"), ("P2", "1"))),),
        ),
        footprint_irs={"EDGEPAD": fp},
        outline=Rectangle(min=Point(0, 0), max=Point(20_000_000, height_nm)),
        stackup=("F.Cu", "B.Cu"),
        design_rules=(
            DesignRules()
            if edge_clearance_nm is None
            else DesignRules(edge_clearance_nm=edge_clearance_nm)
        ),
    )
    board = build_board(spec)
    board = board.with_placement("P1", 4_000_000, pad_centre_y_nm, 0, "F.Cu")
    board = board.with_placement("P2", 16_000_000, pad_centre_y_nm, 0, "F.Cu")
    return export_dsn(board)


def _unprice_edge_class(dsn: str) -> str:
    """Strip the pair values, keep the class name — the one emission the
    production exporter must never produce, pinned here to show why.

    An unpriced class resolves to ``max(getNo, 0)`` = 0: the outline
    loses its clearance outright, the built-in 500 um override opts out,
    and the board routes with *zero* violations where the default
    blocks. Naming the class is an obligation swap, not a win (#84)."""
    return re.sub(r"\n\t\t\t\(clearance \d+ \(type \w+_edge\)\)", "", dsn)


_EDGE_TUPLE = re.compile(
    r"final score: [\d.]+ \((\d+) unrouted and (\d+) violations?\)"
)


def _route_leg(work: Path, dsn: str, timeout_s=120):
    """The route leg only — no SES, no DRC; the question is
    router-internal. Returns the ``(state, unrouted, violations)``
    reading (violations singular in the log at 1 — the existing
    plural-only regex would miss it)."""
    work.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(JAR.read_bytes()).hexdigest()
    if digest != JAR_SHA256:
        pytest.fail(f"{JAR} sha256 {digest} != pinned {JAR_SHA256}")
    dsn_path = work / "board.dsn"
    dsn_path.write_text(dsn)
    route = subprocess.run(
        [
            "timeout",
            str(timeout_s),
            "java",
            "-jar",
            str(JAR),
            "-de",
            str(dsn_path),
            "-do",
            str(work / "board.ses"),
            "--gui.enabled=false",
            "--api_server.enabled=false",
            "-da",
            f"--user_data_path={work / 'ud'}",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    log = route.stdout + route.stderr
    states = _STATE.findall(log)
    scores = _EDGE_TUPLE.findall(log)
    assert scores, f"no final score tuple in the router log:\n{log[-2000:]}"
    unrouted, violations = map(int, scores[-1])
    return (states[-1] if states else "?", unrouted, violations)


@pytest.fixture(scope="module")
def edge_precedence(tmp_path_factory):
    """The five legs, run once — all on the production export (#91).
    Tuples are pinned observations, not smoothed: the pad violations that
    persist even when routed are the fixed pads against the outline
    keepout — which behaves ~100 um fatter than the matrix value
    (violation threshold (575, 600] um under the 500 um default, probed
    on a lone pad) — a router-internal counting quirk recorded on #84;
    routedness is the discriminator."""
    base = tmp_path_factory.mktemp("edge-precedence")
    strip = _edge_probe_board(600_000, 1_200_000)  # pads 350 um off the edge
    wide = _edge_probe_board(800_000, 1_600_000)  # pads 550 um off the edge
    legs = {}
    for name, workdir, dsn in (
        ("default_blocks", "b", strip),
        ("class_300_routes", "a", _edge_probe_board(600_000, 1_200_000, 300_000)),
        ("unpriced_class_zeroes", "d", _unprice_edge_class(strip)),
        ("default_allows", "cc", wide),
        ("class_800_blocks", "c", _edge_probe_board(800_000, 1_600_000, 800_000)),
    ):
        legs[name] = _route_leg(base / workdir, dsn)
    for name in legs:
        assert legs[name][0] == "COMPLETED", f"leg {name} did not complete"
    return legs


class TestDsnEdgeClearancePrecedence:
    """#84 — the emitted outline clearance_class beats the jar's
    built-in 500 um copper-to-edge default, in both directions."""

    def test_the_built_in_default_blocks_the_strip(self, edge_precedence):
        """Control: no class named, the override's 500 um applies — the
        net is unroutable and the pads themselves violate."""
        assert edge_precedence["default_blocks"][1:] == (1, 2)

    def test_a_sub_default_class_routes_where_the_default_could_not(
        self, edge_precedence
    ):
        """300 um honoured: same geometry, the straight shot is legal —
        the emitted class replaced the 500 um default."""
        assert edge_precedence["class_300_routes"][1:] == (0, 2)

    def test_a_super_default_class_blocks_where_the_default_passed(
        self, edge_precedence
    ):
        """800 um honoured: geometry the 500 um default routes (see
        default_allows) becomes unroutable — no clamping to the
        built-in. The reverse direction is what rules out 'parsed but
        ignored'."""
        assert edge_precedence["default_allows"][1:] == (0, 2)
        assert edge_precedence["class_800_blocks"][1:] == (1, 2)

    def test_naming_the_class_without_pricing_it_zeroes_the_edge(self, edge_precedence):
        """The #91 obligation: an unpriced class resolves to index 0 —
        the outline loses its clearance outright and the built-in
        override opts out. Routed with *zero* violations on geometry
        the default blocks: the emission must always pair the class
        with its values."""
        assert edge_precedence["unpriced_class_zeroes"][1:] == (0, 0)
