"""S3 — with_placement: the single mutation, an upsert returning a new Board (ADR-0003)."""

import dataclasses

import pytest

from conftest import FOOTPRINT_IRS, fresh_board, make_spec
from net2board.build import build_board
from net2board.geometry import Point
from net2board.ir import CompIR, NetlistIR
from net2board.model import (
    Constraint,
    Edge,
    EdgeMount,
    Footprint,
    Group,
    Placement,
    Proximity,
    Requirement,
    UnknownConstraintRef,
)

X = Y = 10_000_000


@pytest.fixture
def board():
    return fresh_board()


class TestUpsert:
    def test_new_ref_places(self, board):
        placed = board.with_placement("R1", X, Y, 0, "F.Cu")
        assert len(placed.placements) == 1
        placement = placed.placements[0]
        assert placement.ref == "R1"
        assert placement.pos == Point(X, Y)
        assert placement.rotation == 0
        assert placement.side == "F.Cu"

    def test_existing_ref_relocates(self, board):
        placed = board.with_placement("R1", X, Y, 0, "F.Cu")
        moved = placed.with_placement("R1", 20_000_000, 5_000_000, 90, "B.Cu")
        assert len(moved.placements) == 1
        assert moved.placements[0].pos == Point(20_000_000, 5_000_000)
        assert moved.placements[0].rotation == 90
        assert moved.placements[0].side == "B.Cu"

    def test_relocation_keeps_tuple_position(self, board):
        placed = (
            board.with_placement("R1", X, Y, 0, "F.Cu")
            .with_placement("R2", X, Y, 0, "F.Cu")
            .with_placement("R3", X, Y, 0, "F.Cu")
        )
        moved = placed.with_placement("R2", 0, 0, 180, "B.Cu")
        assert [p.ref for p in moved.placements] == ["R1", "R2", "R3"]

    def test_placed_footprint_is_embedded(self, board):
        placed = board.with_placement("R3", X, Y, 270, "B.Cu")
        (placement,) = placed.placements
        assert placement.footprint.entry_name == "BIGRES"
        assert placement.footprint.courtyard == FOOTPRINT_IRS["BIGRES"].courtyard
        assert [pad.number for pad in placement.footprint.pads] == ["1", "2"]


class TestNewBoard:
    def test_returns_a_new_board(self, board):
        placed = board.with_placement("R1", X, Y, 0, "F.Cu")
        assert placed is not board

    def test_original_board_is_unchanged(self, board):
        board.with_placement("R1", X, Y, 0, "F.Cu")
        assert board.placements == ()

    def test_all_rotations_accepted(self, board):
        for rotation in (0, 90, 180, 270):
            placed = board.with_placement("R1", X, Y, rotation, "F.Cu")
            assert placed.placements[0].rotation == rotation

    def test_both_sides_accepted(self, board):
        assert board.with_placement("R1", X, Y, 0, "F.Cu").placements[0].side == "F.Cu"
        assert board.with_placement("R1", X, Y, 0, "B.Cu").placements[0].side == "B.Cu"


class TestPayloadAliasing:
    """ADR-0001 as amended by #29 — a new Board shares the payload by
    reference and copies only the placement spine. This is what makes a
    retained snapshot cost 3.39 KiB instead of a full board (the measured
    62x); per-placement footprint copies would erase it silently.
    """

    def test_untouched_placements_are_shared(self, board):
        placed = board.with_placement("R1", X, Y, 0, "F.Cu").with_placement(
            "R2", X, Y, 90, "F.Cu"
        )
        moved = placed.with_placement("R1", 20_000_000, 5_000_000, 180, "B.Cu")
        assert moved.placements[1] is placed.placements[1]

    def test_payload_collections_are_shared(self, board):
        placed = board.with_placement("R1", X, Y, 0, "F.Cu")
        assert placed.components is board.components
        assert placed.nets is board.nets
        assert placed.outline is board.outline
        assert placed.stackup is board.stackup

    def test_placed_footprint_aliases_the_components(self, board):
        placed = board.with_placement("R1", X, Y, 0, "F.Cu")
        comp_fp = next(c.footprint for c in board.components if c.ref == "R1")
        assert placed.placements[0].footprint is comp_fp


class TestValidation:
    def test_unknown_ref_raises_immediately(self, board):
        with pytest.raises(ValueError, match="X99"):
            board.with_placement("X99", X, Y, 0, "F.Cu")

    def test_non_90_rotation_raises(self, board):
        with pytest.raises(ValueError, match="rotation"):
            board.with_placement("R1", X, Y, 45, "F.Cu")

    def test_negative_rotation_raises(self, board):
        with pytest.raises(ValueError, match="rotation"):
            board.with_placement("R1", X, Y, -90, "F.Cu")

    def test_bad_side_raises(self, board):
        with pytest.raises(ValueError, match="side"):
            board.with_placement("R1", X, Y, 0, "top")

    def test_board_unchanged_after_failed_mutation(self, board):
        with pytest.raises(ValueError):
            board.with_placement("X99", X, Y, 0, "F.Cu")
        assert board.placements == ()


class TestLocked:
    """ADR-0012: ``locked`` is mobility between Boards — carried on the
    Placement, never intent on one Board — set through the existing
    upsert as a defaulted keyword, leaving M1 call sites untouched."""

    def test_defaults_unlocked(self, board):
        placed = board.with_placement("R1", X, Y, 0, "F.Cu")
        assert placed.placements[0].locked is False

    def test_locked_keyword_lands_on_the_placement(self, board):
        placed = board.with_placement("R1", X, Y, 0, "F.Cu", locked=True)
        assert placed.placements[0].locked is True

    def test_locked_is_part_of_placement_equality(self, board):
        free = board.with_placement("R1", X, Y, 0, "F.Cu")
        locked = board.with_placement("R1", X, Y, 0, "F.Cu", locked=True)
        assert free != locked

    def test_relocation_restates_the_lock(self, board):
        """The upsert replaces the whole placement — relocating without
        the keyword unlocks; restating it keeps the part committed."""
        locked_board = board.with_placement("R1", X, Y, 0, "F.Cu", locked=True)
        moved = locked_board.with_placement("R1", 20_000_000, Y, 0, "F.Cu")
        assert moved.placements[0].locked is False
        kept = locked_board.with_placement("R1", 20_000_000, Y, 0, "F.Cu", locked=True)
        assert kept.placements[0].locked is True

    def test_original_board_is_unchanged(self, board):
        board.with_placement("R1", X, Y, 0, "F.Cu", locked=True)
        assert board.placements == ()


class _CountingRef(str):
    """A ``str`` that tallies the equality and hashing a ref lookup costs."""

    eq_calls = 0
    hash_calls = 0

    def __eq__(self, other):
        type(self).eq_calls += 1
        return str.__eq__(self, other)

    def __hash__(self):
        type(self).hash_calls += 1
        return str.__hash__(self)


def _counting_board(n):
    """An unplaced Board of ``n`` components whose refs are _CountingRef."""
    comps = tuple(CompIR(ref=_CountingRef(f"R{i}"), entry_name="RES") for i in range(n))
    spec = make_spec(netlist_ir=NetlistIR(comps=comps, nets=()))
    return build_board(spec)


class TestRefLookupComplexity:
    """#38 — with_placement's validation and relocation must not grow with
    the number of components or placements (ADR-0001 as amended by #29).

    The assertion is structural, not a wall clock: a counting ``str``
    tallies the ``__eq__``/``__hash__`` operations one ``with_placement``
    call performs, and the count must be flat as the board scales. The
    pre-#38 scans cost ~1.5n equalities per call; the bound below is the
    constant a dict probe pays.
    """

    @staticmethod
    def _measure(n: int, relocate: bool):
        board = _counting_board(n)
        if relocate:
            for i in range(n):
                board = board.with_placement(_CountingRef(f"R{i}"), X, Y, 0, "F.Cu")
        _CountingRef.eq_calls = 0
        _CountingRef.hash_calls = 0
        target = f"R{n // 2}"
        moved = board.with_placement(_CountingRef(target), X, Y, 90, "B.Cu")
        return _CountingRef.eq_calls, _CountingRef.hash_calls, moved, target

    @staticmethod
    def _assert_flat(small, large):
        """Both sizes pay the same, tiny, operation count."""
        assert small[:2] == large[:2]
        assert small[0] <= 8
        assert small[1] <= 8

    @staticmethod
    def _assert_landed(placement, target):
        assert placement.ref == target
        assert placement.pos == Point(X, Y)
        assert placement.rotation == 90
        assert placement.side == "B.Cu"

    def test_place_new_costs_flat_operations(self):
        small = self._measure(64, relocate=False)
        large = self._measure(256, relocate=False)
        self._assert_flat(small, large)
        (placement,) = large[2].placements
        self._assert_landed(placement, large[3])

    def test_relocation_costs_flat_operations(self):
        small = self._measure(64, relocate=True)
        large = self._measure(256, relocate=True)
        self._assert_flat(small, large)
        for n, (_, _, moved, target) in ((64, small), (256, large)):
            self._assert_landed(moved.placements[n // 2], target)
            assert len(moved.placements) == n


class TestPlacementValue:
    """Placement validates itself at construction — the transform and the
    checks may assume a 90° increment and a canonical side, so a direct
    construction can never silently measure as rotation 0 on F.Cu."""

    @staticmethod
    def _placement(**overrides):
        fields = {
            "ref": "R1",
            "footprint": Footprint(entry_name="RES", pads=(), courtyard=None),
            "pos": Point(X, Y),
            "rotation": 0,
            "side": "F.Cu",
        }
        fields.update(overrides)
        return Placement(**fields)

    def test_every_increment_and_side_accepted(self):
        for rotation in (0, 90, 180, 270):
            assert self._placement(rotation=rotation).rotation == rotation
        for side in ("F.Cu", "B.Cu"):
            assert self._placement(side=side).side == side

    def test_non_90_rotation_rejected(self):
        with pytest.raises(ValueError, match="rotation"):
            self._placement(rotation=45)

    def test_negative_rotation_rejected(self):
        with pytest.raises(ValueError, match="rotation"):
            self._placement(rotation=-90)

    def test_bad_side_rejected(self):
        with pytest.raises(ValueError, match="side"):
            self._placement(side="top")


class TestRefReads:
    """The Board's O(1) index reads: consumers look a placement or a
    component up, they do not scan (ADR-0001 as amended by #29)."""

    def test_placement_read_hits_and_misses(self, board):
        placed = board.with_placement("R1", X, Y, 0, "F.Cu")
        assert placed.placement("R1") == placed.placements[0]
        assert placed.placement("R2") is None

    def test_placement_read_tracks_relocation(self, board):
        moved = board.with_placement("R1", X, Y, 0, "F.Cu").with_placement(
            "R1", 0, 0, 90, "B.Cu"
        )
        assert moved.placement("R1").pos == Point(0, 0)

    def test_component_read_hits_and_misses(self, board):
        assert board.component("R1") is not None
        assert board.component("R1").ref == "R1"
        assert board.component("X99") is None

    def test_component_read_survives_placing(self, board):
        component = board.component("R1")
        placed = board.with_placement("R1", X, Y, 0, "F.Cu")
        assert placed.component("R1") is component


class TestConstructionDoors:
    """A published frozen dataclass has two doors: the mutations (which
    share indexes) and plain construction — ``dataclasses.replace``
    included — which must be safe by construction: indexes rebuilt from
    the payload, stated intent re-resolved."""

    def test_replace_rebuilds_the_indexes(self, board):
        placed = board.with_placement("R1", X, Y, 0, "F.Cu").with_placement(
            "R2", 20_000_000, Y, 0, "F.Cu"
        )
        trimmed = dataclasses.replace(placed, placements=placed.placements[:1])
        assert trimmed.placement("R2") is None
        relocated = trimmed.with_placement("R1", 0, 0, 90, "B.Cu")
        assert relocated.placement("R1").pos == Point(0, 0)
        assert [p.ref for p in relocated.placements] == ["R1"]

    def test_replace_then_append_lands_cleanly(self, board):
        placed = board.with_placement("R1", X, Y, 0, "F.Cu")
        trimmed = dataclasses.replace(placed, placements=())
        regrown = trimmed.with_placement("R1", X, Y, 0, "F.Cu")
        assert [p.ref for p in regrown.placements] == ["R1"]

    def test_construction_revalidates_group_anchors(self, board):
        grouped = board.with_group(Group(id="g1", refs=("R1",)))
        with pytest.raises(UnknownConstraintRef):
            dataclasses.replace(
                grouped,
                constraints=(
                    Constraint(
                        id="bad",
                        kind=Requirement(),
                        relation=EdgeMount(
                            target="group:ghost", edge=Edge.LEFT, max_overhang_nm=None
                        ),
                    ),
                ),
            )

    def test_construction_revalidates_placement_anchors(self, board):
        with pytest.raises(UnknownConstraintRef):
            dataclasses.replace(
                board,
                constraints=(
                    Constraint(
                        id="bad",
                        kind=Requirement(),
                        relation=Proximity(
                            ref_a="X99", pad_a="1", ref_b="R1", pad_b="1", bound_nm=1
                        ),
                    ),
                ),
            )
