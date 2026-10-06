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
worse than the hand-tuned 3; tiny_tapeout stays report-only until a
hand-authored baseline exists for it).

Two probe-pinned contract details the chain depends on:

- The SES **placement echo is dropped** before import:
  ``ImportSpecctraSES`` re-places components from it and corrupts
  back-side worlds (C1 measured: pads moved 10 mm at rot 0, shorting
  two nets). The echo carries nothing the board doesn't already hold —
  the placement was ours.
- The router's ``(N unrouted …)`` maps onto KiCad's ratsnest: the
  imported copper leaves exactly N unconnected items.

Deselected by default (``pytest -m slow`` opts in); auto-skips when the
jar, java, system python3+pcbnew, or kicad-cli are absent. A present
jar with the wrong sha256 is a hard failure, not a skip — the pin is
the determinism guarantee (byte-identical SES within one jar version).
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
from collections import Counter
from pathlib import Path

import pytest

from conftest import load_ecc83_spec
from net2board.build import build_board
from net2board.examples.ecc83_placements import apply_placements
from net2board.examples.session import run_session
from net2board.export import export_dsn, export_pcb

JAR = Path(
    os.environ.get("NET2BOARD_FREEROUTING_JAR", "~/tools/freerouting-2.4.1.jar")
).expanduser()
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


pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(
        not JAR.exists() or shutil.which("java") is None,
        reason="freerouting jar or java absent — the slow tier is a local "
        "sign-off step, run deliberately with `pytest -m slow` (ADR-0017)",
    ),
    pytest.mark.skipif(
        _PCBNEW_PY is None or shutil.which("kicad-cli") is None,
        reason="system python3+pcbnew or kicad-cli absent — the SES re-import "
        "and DRC legs need the installed KiCad toolchain",
    ),
]


def _route_chain(work, dsn_path, pcb_path):
    """Route ``dsn_path`` headlessly and re-import the SES onto
    ``pcb_path``, DRC-ing both sides. Everything downstream asserts on
    the returned dict."""
    digest = hashlib.sha256(JAR.read_bytes()).hexdigest()
    if digest != JAR_SHA256:
        pytest.fail(f"{JAR} sha256 {digest} != pinned {JAR_SHA256}")

    ses_path = work / "board.ses"
    user_data = work / "ud"
    route = subprocess.run(
        [
            "timeout",
            "300",
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
