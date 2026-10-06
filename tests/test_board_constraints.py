"""Intent on the Board: the one-upsert mutation surface (ADR-0003, ADR-0011).

``with_constraint``/``without_constraint``/``with_group`` author intent
the way ``with_placement`` authors geometry — one upsert per decision,
each returning a new Board, the old snapshot untouched. Unplaced refs
are legal anchors (an unplaced anchor makes a constraint *pending*, not
invalid — ADR-0011), so references resolve against the netlist's
components, never against ``placements``. Groups resolve against the
Board's groups, nets against the netlist.
"""

import pytest

from conftest import fresh_board
from net2board.geometry import Point, Rectangle
from net2board.model import (
    Constraint,
    ConstraintError,
    Criticality,
    Edge,
    EdgeMount,
    Group,
    KeepTogether,
    Preference,
    Proximity,
    Region,
    Requirement,
    UnknownConstraintId,
    UnknownConstraintRef,
)

X = Y = 10_000_000
RECT = Rectangle(min=Point(0, 0), max=Point(10_000_000, 10_000_000))


def proximity(id: str = "near", **overrides) -> Constraint:
    fields = {
        "ref_a": "R1",
        "pad_a": "1",
        "ref_b": "R2",
        "pad_b": "2",
        "bound_nm": 2_000_000,
    }
    fields.update(overrides)
    return Constraint(id=id, kind=Requirement(), relation=Proximity(**fields))


def edge_mount(id: str = "mount", target: str = "placement:R1") -> Constraint:
    return Constraint(
        id=id,
        kind=Requirement(),
        relation=EdgeMount(target=target, edge=Edge.RIGHT, max_overhang_nm=550_000),
    )


def region(id: str = "region", target: str = "placement:R1") -> Constraint:
    return Constraint(
        id=id, kind=Requirement(), relation=Region(target=target, rect=RECT)
    )


def keep(id: str = "keep", target: str = "group:chips") -> Constraint:
    return Constraint(
        id=id,
        kind=Requirement(),
        relation=KeepTogether(
            target=target, max_width_nm=5_000_000, max_height_nm=5_000_000
        ),
    )


def criticality(id: str = "gnd", net: str = "GND") -> Constraint:
    return Constraint(id=id, kind=Preference(), relation=Criticality(net=net), weight=0)


def chips_group() -> Group:
    return Group(id="chips", refs=("R1", "R2"))


@pytest.fixture
def board():
    return fresh_board()


class TestWithConstraintUpsert:
    def test_states_one_constraint(self, board):
        stated = board.with_constraint(proximity())
        assert stated.constraints == (proximity(),)
        assert len(stated.constraints) == 1

    def test_original_board_is_unchanged(self, board):
        board.with_constraint(proximity())
        assert board.constraints == ()

    def test_re_stating_the_same_constraint_is_idempotent(self, board):
        once = board.with_constraint(proximity())
        twice = once.with_constraint(proximity())
        assert twice == once
        assert len(twice.constraints) == 1

    def test_same_id_different_content_replaces(self, board):
        stated = board.with_constraint(proximity())
        tighter = board.with_constraint(proximity(bound_nm=1_000_000))
        replaced = stated.with_constraint(proximity(bound_nm=1_000_000))
        assert replaced.constraints == (tighter.constraints[0],)
        assert len(replaced.constraints) == 1

    def test_replace_keeps_tuple_position(self, board):
        stated = (
            board.with_constraint(proximity(id="a"))
            .with_constraint(proximity(id="b"))
            .with_constraint(proximity(id="c"))
        )
        replaced = stated.with_constraint(proximity(id="b", bound_nm=500_000))
        assert [c.id for c in replaced.constraints] == ["a", "b", "c"]
        assert replaced.constraints[1].relation.bound_nm == 500_000

    def test_new_ids_append_in_order(self, board):
        stated = board.with_constraint(proximity(id="a")).with_constraint(
            proximity(id="b")
        )
        assert [c.id for c in stated.constraints] == ["a", "b"]


class TestWithoutConstraint:
    def test_removes_by_id(self, board):
        stated = (
            board.with_constraint(proximity(id="a"))
            .with_constraint(proximity(id="b"))
            .without_constraint("a")
        )
        assert [c.id for c in stated.constraints] == ["b"]

    def test_unknown_id_raises(self, board):
        with pytest.raises(UnknownConstraintId, match="ghost") as excinfo:
            board.without_constraint("ghost")
        assert excinfo.value.id == "ghost"

    def test_unknown_id_leaves_the_board_unchanged(self, board):
        stated = board.with_constraint(proximity(id="a"))
        with pytest.raises(UnknownConstraintId):
            stated.without_constraint("ghost")
        assert len(stated.constraints) == 1

    def test_removing_from_a_fresh_board_raises(self, board):
        with pytest.raises(UnknownConstraintId):
            board.without_constraint("a")

    def test_state_then_withdraw_returns_to_the_earlier_snapshot(self, board):
        placed = board.with_placement("R1", X, Y, 0, "F.Cu")
        stated = placed.with_constraint(proximity())
        assert stated.without_constraint("near") == placed


class TestConstraintValidation:
    """References resolve against the netlist's components (pending, not
    invalid, when unplaced — ADR-0011); groups against the Board's
    groups; nets against the netlist. Failures name the anchor."""

    def test_unknown_proximity_ref_rejected(self, board):
        with pytest.raises(UnknownConstraintRef) as excinfo:
            board.with_constraint(proximity(ref_a="X99", pad_a="1"))
        assert excinfo.value.anchor == "placement:X99"

    def test_unknown_proximity_pad_rejected(self, board):
        with pytest.raises(ConstraintError, match="'7'") as excinfo:
            board.with_constraint(proximity(pad_a="7"))
        assert "R1" in str(excinfo.value)

    def test_unknown_edge_mount_ref_rejected(self, board):
        with pytest.raises(UnknownConstraintRef) as excinfo:
            board.with_constraint(edge_mount(target="placement:X99"))
        assert excinfo.value.anchor == "placement:X99"

    def test_unplaced_refs_are_valid_anchors(self, board):
        """Pending, not invalid — every netlist ref resolves (ADR-0011)."""
        stated = board.with_constraint(edge_mount())
        assert [c.id for c in stated.constraints] == ["mount"]

    def test_unknown_region_ref_rejected(self, board):
        with pytest.raises(UnknownConstraintRef) as excinfo:
            board.with_constraint(region(target="placement:X99"))
        assert excinfo.value.anchor == "placement:X99"

    def test_unknown_region_group_rejected(self, board):
        with pytest.raises(UnknownConstraintRef) as excinfo:
            board.with_constraint(region(target="group:ghost"))
        assert excinfo.value.anchor == "group:ghost"

    def test_unknown_keep_together_group_rejected(self, board):
        with pytest.raises(UnknownConstraintRef) as excinfo:
            board.with_constraint(keep(target="group:ghost"))
        assert excinfo.value.anchor == "group:ghost"

    def test_stated_group_satisfies_keep_together(self, board):
        stated = board.with_group(chips_group()).with_constraint(keep())
        assert [c.id for c in stated.constraints] == ["keep"]

    def test_unknown_criticality_net_rejected(self, board):
        with pytest.raises(UnknownConstraintRef) as excinfo:
            board.with_constraint(criticality(net="NOPE"))
        assert excinfo.value.anchor == "net:NOPE"

    def test_failed_mutation_leaves_the_board_unchanged(self, board):
        with pytest.raises(UnknownConstraintRef):
            board.with_constraint(region(target="group:ghost"))
        assert board.constraints == ()


class TestWithGroup:
    def test_states_a_group(self, board):
        stated = board.with_group(chips_group())
        assert stated.groups == (chips_group(),)
        assert board.groups == ()

    def test_upsert_by_id_replaces_in_place(self, board):
        stated = (
            board.with_group(chips_group())
            .with_group(Group(id="other", refs=("R3",)))
            .with_group(Group(id="chips", refs=("R1",)))
        )
        assert [g.id for g in stated.groups] == ["chips", "other"]
        assert stated.groups[0].refs == ("R1",)

    def test_unknown_ref_rejected(self, board):
        with pytest.raises(UnknownConstraintRef) as excinfo:
            board.with_group(Group(id="g", refs=("R1", "X99")))
        assert excinfo.value.anchor == "placement:X99"

    def test_unplaced_refs_are_valid_group_members(self, board):
        stated = board.with_group(chips_group())
        assert stated.groups[0].refs == ("R1", "R2")

    def test_groups_are_canonical_on_the_board(self, board):
        stated = board.with_group(Group(id="g", refs=("R2", "R1")))
        assert stated.groups[0].refs == ("R1", "R2")


class TestIntentSnapshotSemantics:
    """ADR-0011's consequence: Board equality/hash includes constraints
    and groups — identical geometry with different intent is a
    different value."""

    def test_intent_is_part_of_equality(self, board):
        plain = board.with_placement("R1", X, Y, 0, "F.Cu")
        constrained = plain.with_constraint(proximity())
        assert constrained != plain

    def test_groups_are_part_of_equality(self, board):
        plain = board.with_placement("R1", X, Y, 0, "F.Cu")
        grouped = plain.with_group(chips_group())
        assert grouped != plain

    def test_snapshots_hash_consistently(self, board):
        def replay(b):
            return (
                b.with_group(chips_group())
                .with_constraint(proximity(id="a"))
                .with_constraint(criticality())
                .without_constraint("a")
            )

        assert hash(replay(fresh_board())) == hash(replay(fresh_board()))

    def test_branches_with_different_intent_differ(self, board):
        base = board.with_constraint(proximity(id="a"))
        left = base.with_constraint(proximity(id="b"))
        right = base.with_constraint(proximity(id="c"))
        assert left != right

    def test_untouched_payloads_are_shared(self, board):
        stated = board.with_constraint(proximity())
        assert stated.components is board.components
        assert stated.nets is board.nets
        assert stated.outline is board.outline
        assert stated.stackup is board.stackup
        assert stated.placements is board.placements

    def test_placement_mutation_shares_the_intent(self, board):
        stated = board.with_constraint(proximity())
        placed = stated.with_placement("R1", X, Y, 0, "F.Cu")
        assert placed.constraints is stated.constraints
        assert placed.groups is stated.groups

    def test_intent_mutation_shares_the_geometry(self, board):
        placed = board.with_placement("R1", X, Y, 0, "F.Cu")
        stated = placed.with_constraint(proximity())
        assert stated.placements is placed.placements

    def test_intent_mutation_shares_the_other_namespace(self, board):
        stated = board.with_group(chips_group())
        constrained = stated.with_constraint(proximity())
        assert constrained.groups is stated.groups
        grouped = constrained.with_group(Group(id="g2", refs=("R3",)))
        assert grouped.constraints is constrained.constraints
