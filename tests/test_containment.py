"""Closed containment predicates: shape_contained_in (ADR-0018, ADR-0006).

Containment is closed — boundary contact allowed; a courtyard tangent to
the outline edge is inside (C2 sits tangent and stays clean), consistent
with overlap's touching-never-violates (ADR-0006) and Region's closed
membership (ADR-0011). Every case is decided in exact-integer arithmetic:
rect-in-rect is four ``<=`` comparisons; circle-in-rect is per-edge
sign-checked squared distance against ``radius_sq`` — no ``isqrt``, no
float (ADR-0005). The boundary cases are the point of this table: touch,
one nm out, fully in.
"""

from net2board.geometry import Circle, Point, Rectangle, shape_contained_in


def rect(x0, y0, x1, y1):
    return Rectangle(min=Point(x0, y0), max=Point(x1, y1))


def circle(cx, cy, radius_sq):
    # `end` carries the defining radius vector for export (ADR-0010); the
    # containment predicates never read it, so it is inert here.
    return Circle(center=Point(cx, cy), radius_sq=radius_sq, end=Point(cx, cy))


OUTLINE = rect(0, 0, 10, 10)


class TestRectInRect:
    def test_fully_inside_with_slack(self):
        assert shape_contained_in(rect(2, 2, 3, 3), OUTLINE)

    def test_touching_one_edge_is_inside(self):
        assert shape_contained_in(rect(0, 2, 5, 5), OUTLINE)

    def test_touching_corner_is_inside(self):
        assert shape_contained_in(rect(5, 5, 10, 10), OUTLINE)

    def test_identical_is_inside(self):
        assert shape_contained_in(OUTLINE, OUTLINE)

    def test_one_nm_outside_is_outside(self):
        assert not shape_contained_in(rect(0, 0, 11, 10), OUTLINE)

    def test_poking_corner_is_outside(self):
        assert not shape_contained_in(rect(-1, -1, 5, 5), OUTLINE)

    def test_surrounding_inner_is_outside(self):
        assert not shape_contained_in(rect(-1, -1, 11, 11), OUTLINE)

    def test_fully_outside_is_outside(self):
        assert not shape_contained_in(rect(20, 20, 30, 30), OUTLINE)


class TestCircleInRect:
    def test_fully_inside_with_slack(self):
        assert shape_contained_in(circle(0, 0, 9), rect(-4, -4, 4, 4))

    def test_tangent_to_all_four_edges_is_inside(self):
        # d = 3 per edge, d² = 9 = radius_sq: closed containment (ADR-0018)
        assert shape_contained_in(circle(0, 0, 9), rect(-3, -3, 3, 3))

    def test_tangent_to_one_edge_is_inside(self):
        assert shape_contained_in(circle(0, 0, 9), rect(-3, -3, 10, 3))

    def test_one_nm_outside_is_outside(self):
        assert not shape_contained_in(circle(0, 0, 9), rect(-2, -3, 3, 3))

    def test_center_on_the_edge_is_outside(self):
        assert not shape_contained_in(circle(0, 0, 9), rect(0, -3, 3, 3))

    def test_circle_enclosing_the_rect_is_outside(self):
        assert not shape_contained_in(circle(0, 0, 900), rect(-3, -3, 3, 3))


class TestExactIntegerBoundaries:
    # radius_sq 10: radius √10 ≈ 3.162 — not an integer in nm, so the
    # squared comparison is the only exact judge (ADR-0005).

    def test_sub_nm_poke_is_outside(self):
        # d = 3 < √10: the circle pokes ~0.162 nm past the edge
        assert not shape_contained_in(circle(0, 0, 10), rect(-3, -3, 3, 3))

    def test_one_nm_slack_is_inside(self):
        # d = 4 ≥ √10 per edge
        assert shape_contained_in(circle(0, 0, 10), rect(-4, -4, 4, 4))

    def test_sign_check_rejects_center_outside(self):
        # Center beyond the right/top edges: the squared distances to all
        # four edges (100, 16, 100, 16) all exceed radius_sq — only the
        # sign check sees the negative distances.
        assert not shape_contained_in(circle(0, 0, 9), rect(-10, -10, -4, -4))

    def test_off_center_corner_case(self):
        # Tangent bottom-left, slack top-right: d = (3, 4) from min corner,
        # 9 = radius_sq and 16 ≥ 9
        assert shape_contained_in(circle(0, 0, 9), rect(-3, -4, 3, 3))


class TestDispatcher:
    def test_both_shape_variants_dispatch(self):
        assert shape_contained_in(rect(1, 1, 2, 2), OUTLINE)
        assert shape_contained_in(circle(5, 5, 4), OUTLINE)
        assert not shape_contained_in(rect(9, 9, 12, 12), OUTLINE)
        assert not shape_contained_in(circle(5, 5, 64), OUTLINE)  # r = 8 pokes out

    def test_argument_order_is_shape_then_container(self):
        # The second argument is always the container (outline, region):
        # a small shape sits in a big one, never the reverse.
        assert shape_contained_in(rect(0, 0, 10, 10), rect(-5, -5, 15, 15))
        assert not shape_contained_in(rect(-5, -5, 15, 15), rect(0, 0, 10, 10))
