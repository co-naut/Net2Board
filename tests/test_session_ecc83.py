"""The M2 session — fast-tier acceptance over one Board lineage (ADR-0019, #48).

The session module (``net2board.examples.session``) is the single harness:
these tests import ``run_session()`` for the replay face and the byte
goldens — exactly what the runnable example executes, so there is no
second harness to drift. The session starts from the committed acceptance
table (M1's output is M2's input), improves it under solve, states the
relation beats, and demonstrates the failure channels: an under-budgeted
step cut off mid-work and retried, and a deliberate unsatisfiable
Requirement reported and relaxed.

Replay — running the session twice reproduces every step and the export
bytes — is asserted separately from pinning; the goldens in
``tests/goldens/session/`` catch unintended drift on top of it (ADR-0019
keeps the two properties apart).
"""

import dataclasses
import math
import subprocess
import sys

import pytest

from conftest import assert_byte_golden, assert_golden, load_ecc83_spec
from net2board.build import build_board
from net2board.drc import Severity, ViolationType, run_drc
from net2board.examples.ecc83_placements import apply_placements
from net2board.export import export_pcb
from net2board.geometry import pad_world_center, shape_aabb, world_courtyard
from net2board.harness.session import (
    BUDGET_MAIN,
    BUDGET_UNDERCUT,
    SessionRecord,
    SessionStep,
    run_session,
)
from net2board.model import ROTATIONS, Edge, EdgeMount, KeepTogether, Proximity, Region
from net2board.objective import evaluate
from net2board.solver import StopReason, solve

REGEN_HINT = "regenerate deliberately: UPDATE_GOLDENS=1 uv run pytest tests/test_session_ecc83.py"


@pytest.fixture(scope="module")
def record() -> SessionRecord:
    """One session for the read-only assertions; replay runs its own."""
    return run_session()


def record_json(record: SessionRecord) -> dict:
    """The pinned projection: each step's replay record — its name, pinned
    budget and scope, the ``SolveResult`` readings (stop reason, unmet
    ids), the legalize delta, and the Objective term list, asdict-rendered.
    The board itself is held by the export golden."""
    return {
        "baseline_nm": record.baseline_nm,
        "steps": [
            {
                "name": step.name,
                "budget": step.budget,
                "refs": list(step.refs) if step.refs is not None else None,
                "stop_reason": step.stop_reason,
                "candidates_evaluated": step.candidates_evaluated,
                "unmet_constraint_ids": list(step.unmet_constraint_ids),
                "legalize_delta_nm": step.legalize_delta_nm,
                "objective_total_nm": step.objective.total_nm,
                "objective_terms": [
                    dataclasses.asdict(term) for term in step.objective.terms
                ],
            }
            for step in record.steps
        ],
    }


def by_name(record: SessionRecord, name: str) -> SessionStep:
    return next(step for step in record.steps if step.name == name)


def error_findings(board):
    return tuple(v for v in run_drc(board) if v.severity is Severity.ERROR)


def rotations_by_ref(board):
    """The board's rotations by ref — the shape both rotation readings
    diff (the knob's bite in the replay face, turn-optimality in the
    beat face)."""
    return {p.ref: p.rotation for p in board.placements}


class TestReplay:
    def test_two_runs_are_identical_down_to_the_export_bytes(self):
        first = run_session()
        second = run_session()
        assert first == second
        assert first.export == second.export

    def test_the_lineage_walks_seven_solve_steps(self, record):
        assert [step.name for step in record.steps] == [
            "undercut",
            "improve",
            "criticality",
            "relations",
            "unsat",
            "relaxed",
            "rotation",
        ]


class TestKnobOnReplay:
    """The knob-on replay (ADR-0021's falsification): a ``free_rotation``
    solve over the ecc83 baseline replays byte-identically — same
    ``(board, refs, budget, knob)``, same bytes. No golden is pinned
    here: the knob-on candidate reading is #85's pre-registered run;
    what this pins is determinism, plus the bite — the knob-on search
    actually re-poses parts the knob-off search leaves standing, and
    (measured 2026-10-25, #115) demonstrably *turns* one: U1, from 0 to
    270 degrees."""

    @staticmethod
    def _baseline():
        return apply_placements(build_board(load_ecc83_spec()))

    @pytest.fixture(scope="class")
    @classmethod
    def knob_on_runs(cls):
        """Two independent knob-on solves, each from a freshly built
        baseline — the replay pair, shared with the bite tests."""
        runs = []
        for _ in range(2):
            result = solve(cls._baseline(), budget=BUDGET_MAIN, free_rotation=True)
            assert result.stop_reason is StopReason.NO_FURTHER_IMPROVEMENT
            for placement in result.board.placements:
                assert placement.rotation in (0, 90, 180, 270)
            runs.append(result)
        return runs

    @pytest.fixture(scope="class")
    @classmethod
    def knob_off_run(cls):
        """The knob-off solve from the same baseline — the comparison
        the bite tests read."""
        return solve(cls._baseline(), budget=BUDGET_MAIN)

    def test_two_knob_on_runs_are_byte_identical(self, knob_on_runs):
        first, second = knob_on_runs
        assert export_pcb(first.board) == export_pcb(second.board)

    def test_the_knob_actually_reposes_the_baseline(self, knob_on_runs, knob_off_run):
        knob_on = knob_on_runs[0]
        refs = {p.ref for p in knob_on.board.placements} | {
            p.ref for p in knob_off_run.board.placements
        }
        moved = [
            ref
            for ref in sorted(refs)
            if knob_on.board.placement(ref) != knob_off_run.board.placement(ref)
        ]
        assert moved, "the knob changed nothing — the replay proves nothing"
        assert evaluate(knob_on.board).total_nm <= evaluate(knob_off_run.board).total_nm

    def test_the_knob_actually_turns_a_part(self, knob_on_runs, knob_off_run):
        """The rotation bite, pinned as measured (#115): from the
        baseline the knob-on search turns U1 to 270 degrees — a rotation
        no knob-off candidate may carry. A different turn here is a
        re-pin: name the new reading and the date."""
        turned = {
            ref: rotation
            for ref, rotation in rotations_by_ref(knob_on_runs[0].board).items()
            if rotation != rotations_by_ref(knob_off_run.board)[ref]
        }
        assert turned == {"U1": 270}


class TestBudgetBeat:
    """The enum's reason to exist, demonstrated once (ADR-0019): an
    under-budgeted attempt stops exhausted mid-work, and the retry at the
    full pinned budget completes."""

    def test_the_undercut_exhausts_strictly_mid_work(self, record):
        undercut = by_name(record, "undercut")
        improve = by_name(record, "improve")
        assert undercut.stop_reason is StopReason.BUDGET_EXHAUSTED
        assert undercut.budget == BUDGET_UNDERCUT
        # exhausted == candidates were cut mid-scan (#107), so the spend
        # reads the budget exactly — the seam's own count (#102)
        assert undercut.candidates_evaluated == BUDGET_UNDERCUT
        assert record.baseline_nm > undercut.objective.total_nm
        assert undercut.objective.total_nm > improve.objective.total_nm

    def test_every_step_records_its_spend(self, record):
        """The count rides the step (#102): each step's replay record
        carries the Candidates its solve evaluated — never more than the
        step's pinned budget, never zero (every step has real work)."""
        for step in record.steps:
            assert 0 < step.candidates_evaluated <= step.budget

    def test_the_retry_completes_past_the_baseline(self, record):
        improve = by_name(record, "improve")
        assert improve.stop_reason is StopReason.NO_FURTHER_IMPROVEMENT
        assert improve.budget == BUDGET_MAIN
        assert improve.unmet_constraint_ids == ()
        assert improve.objective.total_nm < record.baseline_nm


class TestCriticalityBeat:
    """The GND lever and the hot-net weight, stated as Preferences and
    visible in the Objective terms (ADR-0014)."""

    def test_the_term_list_carries_the_stated_weights(self, record):
        terms = by_name(record, "criticality").objective.terms
        by_ref = {term.ref: term for term in terms}
        assert by_ref["net:GND"].weight == 0
        assert by_ref["net:Net-(U1A-G)"].weight == 4
        assert by_ref["net:Net-(U1A-K)"].weight == 1

    def test_the_step_is_a_clean_reranking(self, record):
        step = by_name(record, "criticality")
        assert step.stop_reason is StopReason.NO_FURTHER_IMPROVEMENT
        assert step.unmet_constraint_ids == ()


class TestRelationBeats:
    """Every relation stated once and honoured: the constraints are on the
    lineage's board, and the geometry measures into each stated bound."""

    def test_the_stated_relations_are_on_the_board(self, record):
        board = by_name(record, "relations").board
        relations = {c.id: type(c.relation) for c in board.constraints}
        assert relations["kt-beat"] is KeepTogether
        assert relations["prox-beat"] is Proximity
        assert relations["region-beat"] is Region
        assert any(isinstance(c.relation, EdgeMount) for c in board.constraints)
        group = next(g for g in board.groups if g.id == "g-beat")
        assert group.refs == ("R1", "R2")

    def test_proximity_measures_within_its_bound(self, record):
        board = by_name(record, "relations").board
        relation = next(c.relation for c in board.constraints if c.id == "prox-beat")
        placements = {p.ref: p for p in board.placements}
        a = pad_world_center(placements["C2"], relation.pad_a)
        b = pad_world_center(placements["U1"], relation.pad_b)
        distance = math.isqrt((a.x - b.x) ** 2 + (a.y - b.y) ** 2)
        assert distance <= relation.bound_nm
        assert relation.same_side
        assert placements["C2"].side == placements["U1"].side

    def test_the_region_gathers_its_target(self, record):
        board = by_name(record, "relations").board
        relation = next(c.relation for c in board.constraints if c.id == "region-beat")
        placement = {p.ref: p for p in board.placements}["C2"]
        yard = shape_aabb(world_courtyard(placement))
        assert relation.rect.min.x <= yard.min.x and yard.max.x <= relation.rect.max.x
        assert relation.rect.min.y <= yard.min.y and yard.max.y <= relation.rect.max.y

    def test_keep_together_holds_the_group(self, record):
        board = by_name(record, "relations").board
        relation = next(c.relation for c in board.constraints if c.id == "kt-beat")
        placements = {p.ref: p for p in board.placements}
        yards = [shape_aabb(world_courtyard(placements[ref])) for ref in ("R1", "R2")]
        width = max(y.max.x for y in yards) - min(y.min.x for y in yards)
        height = max(y.max.y for y in yards) - min(y.min.y for y in yards)
        assert width <= relation.max_width_nm
        assert height <= relation.max_height_nm

    def test_the_relations_step_reports_all_met(self, record):
        step = by_name(record, "relations")
        assert step.stop_reason is StopReason.NO_FURTHER_IMPROVEMENT
        assert step.unmet_constraint_ids == ()


class TestUnsatBeat:
    """A deliberate contradiction — P7 gathered into the bottom band while
    a Requirement mounts it to the top edge — is reported, not wrecked:
    the finding is named, the board is untouched, and the relaxing step
    comes back clean (ADR-0019's detect-and-fix beat in M2's vocabulary)."""

    def test_the_contradiction_is_named_and_finding_led(self, record):
        step = by_name(record, "unsat")
        assert step.unmet_constraint_ids == ("region-bad",)
        findings = error_findings(step.board)
        assert len(findings) == 1
        assert findings[0].type is ViolationType.REGION_UNMET
        assert findings[0].offending_refs[0] == "constraint:region-bad"

    def test_the_board_survives_unsolved(self, record):
        relations = by_name(record, "relations").board
        unsat = by_name(record, "unsat").board
        # the contradiction moves nothing — P7's own Pose is untouched —
        # and the one mover is solve's finalize legalize repairing a pad
        # finding the relations board carried (P2 nudged out of clearance,
        # #91's copper vocabulary): the board survives, more legal, with
        # the contradiction still named
        relations_by_ref = {p.ref: p for p in relations.placements}
        unsat_by_ref = {p.ref: p for p in unsat.placements}
        assert unsat_by_ref["P7"] == relations_by_ref["P7"]
        moved = {
            ref
            for ref, placement in unsat_by_ref.items()
            if placement != relations_by_ref[ref]
        }
        assert moved == {"P2"}

    def test_the_relaxing_step_comes_back_clean(self, record):
        step = by_name(record, "relaxed")
        assert step.stop_reason is StopReason.NO_FURTHER_IMPROVEMENT
        assert step.unmet_constraint_ids == ()
        assert error_findings(step.board) == ()
        assert "region-bad" not in {c.id for c in step.board.constraints}


class TestRotationBeat:
    """The free_rotation beat (ADR-0024's goldens clause): one knob-on
    step closes the session — rotation enters as a beat, never a headline
    change — and its board is the export the GUI sign-off reads (item 5).
    Pinned as measured 2026-10-25 (#115): from the searched optimum the
    knob-on walk changes no rotation — it certifies the board
    turn-optimal — and still improves the objective by a re-ordered
    walk's position find; the face's rotated part is R3, at the committed
    table's own 90 degrees. The turning bite lives in the replay face
    (``TestKnobOnReplay``). The exact candidate and objective readings
    are the record JSON's to pin."""

    def test_the_beat_stops_converged(self, record):
        step = by_name(record, "rotation")
        assert step.stop_reason is StopReason.NO_FURTHER_IMPROVEMENT
        assert step.unmet_constraint_ids == ()

    def test_the_beat_certifies_turn_optimality(self, record):
        """The knob turned nothing: every rotation on the beat board is
        the relaxed board's own. An entry here is a re-pin — name the
        turn, the reading, and the date."""
        relaxed = by_name(record, "relaxed")
        rotation = by_name(record, "rotation")
        before = {p.ref: p.rotation for p in relaxed.board.placements}
        after = {p.ref: p.rotation for p in rotation.board.placements}
        assert after == before, "the knob turned something — re-pin the reading"

    def test_the_beat_face_carries_a_rotated_part(self, record):
        """Sign-off item 5's subject exists: at least one part on the
        exported beat board sits at a non-zero rotation."""
        step = by_name(record, "rotation")
        turned_refs = [ref for ref, r in rotations_by_ref(step.board).items() if r != 0]
        assert turned_refs, "no rotated part on the beat face — item 5 is blind"

    def test_the_beat_never_regresses_the_objective(self, record):
        rotation = by_name(record, "rotation")
        relaxed = by_name(record, "relaxed")
        assert rotation.objective.total_nm <= relaxed.objective.total_nm

    def test_rotations_stay_in_the_model_set(self, record):
        step = by_name(record, "rotation")
        assert {p.rotation for p in step.board.placements} <= set(ROTATIONS)


def protrusions(board):
    """Every courtyard crossing the outline: ``ref -> {Edge: overhang}``.
    The sanctioned rows really do hang past the frame — the containment
    check stays clean only because their Requirement EdgeMounts state it."""
    outline = board.outline
    protruding: dict[str, dict[Edge, int]] = {}
    for placement in board.placements:
        yard = shape_aabb(world_courtyard(placement))
        edges: dict[Edge, int] = {}
        if yard.min.x < outline.min.x:
            edges[Edge.LEFT] = outline.min.x - yard.min.x
        if yard.max.x > outline.max.x:
            edges[Edge.RIGHT] = yard.max.x - outline.max.x
        if yard.min.y < outline.min.y:
            edges[Edge.TOP] = outline.min.y - yard.min.y
        if yard.max.y > outline.max.y:
            edges[Edge.BOTTOM] = yard.max.y - outline.max.y
        if edges:
            protruding[placement.ref] = edges
    return protruding


class TestPerStepInvariants:
    """Asserted by construction inside the session, checked here from the
    record: legalize delta exactly 0, nothing stranded, containment clean
    or sanctioned — and the sanctioned rows demonstrably hanging, in bound
    (ADR-0012, ADR-0013, ADR-0018)."""

    def test_legalize_delta_is_exactly_zero_on_every_step(self, record):
        assert all(step.legalize_delta_nm == 0 for step in record.steps)

    def test_no_part_is_ever_stranded(self, record):
        assert all(step.board.unplaced_refs() == () for step in record.steps)

    def test_containment_stays_clean_or_sanctioned(self, record):
        for step in record.steps:
            types = {v.type for v in run_drc(step.board)}
            assert ViolationType.COURTYARD_OUTSIDE_OUTLINE not in types
            assert ViolationType.EDGE_MOUNT_UNMET not in types

    def test_the_sanctioned_overhangs_hang_and_stay_in_bound(self, record):
        for step in record.steps:
            stated: dict[str, dict[Edge, int]] = {}
            for constraint in step.board.constraints:
                relation = constraint.relation
                if isinstance(relation, EdgeMount) and (
                    relation.max_overhang_nm is not None
                ):
                    ref = relation.target.removeprefix("placement:")
                    stated.setdefault(ref, {})[relation.edge] = relation.max_overhang_nm
            hanging = protrusions(step.board)
            assert hanging, f"{step.name}: no sanctioned row actually overhangs"
            for ref, edges in hanging.items():
                assert ref in stated, f"{step.name}: {ref} overhangs unsanctioned"
                for edge, overhang in edges.items():
                    assert overhang <= stated[ref][edge], (
                        f"{step.name}: {ref} past its {edge.value} bound"
                    )


class TestGoldens:
    def test_export_bytes_are_pinned(self, record):
        assert_byte_golden(
            "session",
            "ecc83_session.kicad_pcb",
            record.export,
            REGEN_HINT,
        )

    def test_the_record_is_pinned(self, record):
        assert_golden("session", "ecc83_session.json", record_json(record), REGEN_HINT)


class TestRunnableExample:
    def test_one_command_writes_the_searched_board(self, tmp_path, record):
        output = tmp_path / "session.kicad_pcb"
        result = subprocess.run(
            [sys.executable, "-m", "net2board.examples.session", str(output)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        assert output.read_bytes() == record.export.encode()
        assert "baseline" in result.stdout
