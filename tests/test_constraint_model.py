"""The intent vocabulary: relations, kinds, Constraint, Group (ADR-0011, ADR-0014).

Construction validates every value-level invariant eagerly — anchor
grammar, kinds, weights, extents — so malformed intent never reaches a
Board. Reference-level validation (does the ref/group/net exist *here*?)
is the mutation's job, pinned in ``test_board_constraints``. The public
names are the CONTEXT.md vocabulary verbatim: Requirement, Preference,
Proximity, Region, EdgeMount, KeepTogether, Criticality.
"""

import pytest

from net2board.errors import Net2BoardError
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

RECT = Rectangle(min=Point(0, 0), max=Point(10_000_000, 10_000_000))


def proximity(**overrides) -> Proximity:
    fields = {
        "ref_a": "R1",
        "pad_a": "1",
        "ref_b": "R2",
        "pad_b": "2",
        "bound_nm": 2_000_000,
    }
    fields.update(overrides)
    return Proximity(**fields)


def constraint(**overrides) -> Constraint:
    fields = {"id": "near", "kind": Requirement(), "relation": proximity()}
    fields.update(overrides)
    return Constraint(**fields)


class TestKinds:
    def test_kind_instances_compare_by_type(self):
        assert Requirement() == Requirement()
        assert Preference() == Preference()
        assert Requirement() != Preference()

    def test_kind_instances_hash(self):
        assert hash(Requirement()) == hash(Requirement())
        assert len({Requirement(), Requirement(), Preference()}) == 2


class TestProximity:
    def test_constructs_with_fields(self):
        relation = proximity(same_side=True)
        assert (relation.ref_a, relation.pad_a) == ("R1", "1")
        assert (relation.ref_b, relation.pad_b) == ("R2", "2")
        assert relation.bound_nm == 2_000_000
        assert relation.same_side is True

    def test_same_side_defaults_false(self):
        assert proximity().same_side is False

    @pytest.mark.parametrize("bound", [-1, 1.5, True, "2mm"])
    def test_bad_bound_rejected(self, bound):
        with pytest.raises(ConstraintError, match="bound_nm"):
            proximity(bound_nm=bound)


class TestRegion:
    def test_constructs_with_target_and_rect(self):
        relation = Region(target="placement:R1", rect=RECT)
        assert relation.target == "placement:R1"
        assert relation.rect == RECT

    def test_unordered_rect_corners_rejected(self):
        inverted = Rectangle(min=Point(10_000_000, 0), max=Point(0, 10_000_000))
        with pytest.raises(ConstraintError, match="rect"):
            Region(target="placement:R1", rect=inverted)

    @pytest.mark.parametrize("target", ["R1", "pad:R1", "placement:", "group:", "", 7])
    def test_malformed_anchor_rejected(self, target):
        with pytest.raises(ConstraintError, match="anchor"):
            Region(target=target, rect=RECT)


class TestEdgeMount:
    def test_overhang_defaults_none(self):
        relation = EdgeMount(target="placement:P1", edge=Edge.RIGHT)
        assert relation.max_overhang_nm is None

    def test_constructs_with_overhang(self):
        relation = EdgeMount(
            target="placement:P1", edge=Edge.TOP, max_overhang_nm=550_000
        )
        assert relation.max_overhang_nm == 550_000

    def test_negative_overhang_rejected(self):
        with pytest.raises(ConstraintError, match="max_overhang_nm"):
            EdgeMount(target="placement:P1", edge=Edge.LEFT, max_overhang_nm=-1)

    def test_edge_must_be_an_edge(self):
        with pytest.raises(ConstraintError, match="edge"):
            EdgeMount(target="placement:P1", edge="right")

    @pytest.mark.parametrize("target", ["R1", "placement:", "group:", ""])
    def test_malformed_anchor_rejected(self, target):
        with pytest.raises(ConstraintError, match="anchor"):
            EdgeMount(target=target, edge=Edge.BOTTOM)


class TestKeepTogether:
    def test_constructs_with_group_target_and_extent(self):
        relation = KeepTogether(
            target="group:decoupling", max_width_nm=5_000_000, max_height_nm=3_000_000
        )
        assert relation.target == "group:decoupling"
        assert relation.max_width_nm == 5_000_000
        assert relation.max_height_nm == 3_000_000

    def test_placement_target_rejected(self):
        """ADR-0011: KeepTogether is the floating extent *over a Group*."""
        with pytest.raises(ConstraintError, match="group"):
            KeepTogether(target="placement:R1", max_width_nm=1, max_height_nm=1)

    @pytest.mark.parametrize("extent", [0, -1, 1.5, True, "5mm"])
    def test_bad_extent_rejected(self, extent):
        with pytest.raises(ConstraintError, match="max_"):
            KeepTogether(target="group:g", max_width_nm=extent, max_height_nm=1_000_000)
        with pytest.raises(ConstraintError, match="max_"):
            KeepTogether(target="group:g", max_width_nm=1_000_000, max_height_nm=extent)


class TestCriticality:
    def test_constructs_preference(self):
        assert Criticality(net="GND").net == "GND"

    def test_empty_net_rejected(self):
        with pytest.raises(ConstraintError, match="net"):
            Criticality(net="")


class TestConstraint:
    def test_constructs_with_defaults(self):
        subject = constraint()
        assert subject.id == "near"
        assert isinstance(subject.kind, Requirement)
        assert subject.weight == 1

    def test_preference_kind_constructs(self):
        subject = constraint(kind=Preference(), weight=0)
        assert isinstance(subject.kind, Preference)
        assert subject.weight == 0

    def test_criticality_as_requirement_rejected(self):
        """ADR-0014: Criticality is the one Preference-only relation."""
        with pytest.raises(ConstraintError, match="Preference-only"):
            constraint(relation=Criticality(net="GND"))

    def test_criticality_as_preference_accepted(self):
        subject = constraint(kind=Preference(), relation=Criticality(net="GND"))
        assert subject.relation == Criticality(net="GND")

    def test_negative_weight_rejected(self):
        with pytest.raises(ConstraintError, match="weight"):
            constraint(weight=-1)

    @pytest.mark.parametrize("bad_id", ["", 7, None])
    def test_bad_id_rejected(self, bad_id):
        with pytest.raises(ConstraintError, match="id"):
            constraint(id=bad_id)

    def test_non_relation_rejected(self):
        with pytest.raises(ConstraintError, match="relation"):
            constraint(relation=RECT)

    def test_unknown_kind_rejected(self):
        with pytest.raises(ConstraintError, match="kind"):
            constraint(kind="requirement")

    @pytest.mark.parametrize(
        "relation",
        [
            proximity(),
            Region(target="placement:R1", rect=RECT),
            EdgeMount(target="placement:R1", edge=Edge.RIGHT),
            KeepTogether(
                target="group:g", max_width_nm=1_000_000, max_height_nm=1_000_000
            ),
        ],
    )
    def test_one_vocabulary_serves_both_kinds(self, relation):
        """ADR-0011: every bounded relation states as either kind — only
        Criticality is the recorded exception (ADR-0014)."""
        assert constraint(kind=Requirement(), relation=relation).relation is relation
        assert constraint(kind=Preference(), relation=relation).relation is relation

    def test_frozen(self):
        with pytest.raises(Exception, match="assign"):
            constraint().id = "other"


class TestGroup:
    def test_constructs_and_canonicalizes_ref_order(self):
        group = Group(id="decoupling", refs=("C2", "C1", "C3"))
        assert group.refs == ("C1", "C2", "C3")

    def test_ref_order_is_not_part_of_intent(self):
        assert Group(id="g", refs=("C2", "C1")) == Group(id="g", refs=("C1", "C2"))

    def test_empty_refs_rejected(self):
        with pytest.raises(ConstraintError, match="refs"):
            Group(id="g", refs=())

    def test_duplicate_refs_rejected(self):
        with pytest.raises(ConstraintError, match="twice"):
            Group(id="g", refs=("C1", "C1"))

    def test_empty_id_rejected(self):
        with pytest.raises(ConstraintError, match="id"):
            Group(id="", refs=("C1",))

    def test_empty_ref_rejected(self):
        with pytest.raises(ConstraintError, match="ref"):
            Group(id="g", refs=("C1", ""))

    def test_frozen(self):
        with pytest.raises(Exception, match="assign"):
            Group(id="g", refs=("C1",)).id = "other"


class TestErrorTree:
    """ADR-0008 posture: structured, frozen-dataclass exceptions under the
    library-wide root, carrying the offending name (ADR-0013's tree)."""

    def test_root_under_net2board_error(self):
        assert issubclass(ConstraintError, Net2BoardError)

    def test_leaves_under_the_root(self):
        assert issubclass(UnknownConstraintId, ConstraintError)
        assert issubclass(UnknownConstraintRef, ConstraintError)

    def test_unknown_id_names_the_id(self):
        error = UnknownConstraintId("ghost")
        assert error.id == "ghost"
        assert "ghost" in str(error)

    def test_unknown_ref_names_the_anchor(self):
        error = UnknownConstraintRef("placement:X99")
        assert error.anchor == "placement:X99"
        assert "placement:X99" in str(error)

    def test_leaves_are_frozen_dataclasses(self):
        with pytest.raises(Exception, match="assign"):
            UnknownConstraintId("ghost").id = "other"
