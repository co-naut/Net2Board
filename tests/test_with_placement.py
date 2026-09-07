"""S3 — with_placement: the single mutation, an upsert returning a new Board (ADR-0003)."""

import pytest

from conftest import FOOTPRINT_IRS, fresh_board
from net2board.geometry import Point

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
