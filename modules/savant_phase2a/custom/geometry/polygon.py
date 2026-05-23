"""Pure geometry utilities for behaviour rules."""

from __future__ import annotations

from typing import List, Tuple


def point_in_polygon(
    point: Tuple[float, float],
    polygon: List[Tuple[float, float]],
) -> bool:
    """Ray-casting point-in-polygon test.

    Args:
        point: ``(x, y)`` in frame coordinates.
        polygon: List of ``(x, y)`` vertices in clockwise or counter-clockwise
            order.  The polygon does **not** need to be explicitly closed (the
            function wraps the last edge to the first vertex automatically).

    Returns:
        ``True`` if *point* is inside the polygon (or on a boundary edge).
    """
    x, y = point
    inside = False
    n = len(polygon)
    if n < 3:
        return False

    j = n - 1
    for i in range(n):
        xi, yi = polygon[i]
        xj, yj = polygon[j]
        # Check if edge crosses the horizontal ray at y
        if (yi > y) != (yj > y):
            x_intersect = (xj - xi) * (y - yi) / (yj - yi) + xi
            if x < x_intersect:
                inside = not inside
        j = i

    return inside
