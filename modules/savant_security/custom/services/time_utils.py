"""Normalize Savant frame PTS to integer milliseconds.

Savant ``frame_meta.pts`` can be in different timebases (nanoseconds,
microseconds, or already milliseconds) depending on the source adapter
and container environment.  This module provides a single conversion
entrypoint so that gate, exporter, and throttle logic all agree on the
same millisecond timestamp.

Contract:
    - Input:  ``frame_meta.pts`` (int or None).
    - Output: ``int`` in milliseconds.

Detection heuristic:
    - If pts >= 1_000_000_000 (i.e. >= 10^9), assume nanoseconds.
    - Else if pts >= 10_000_000, also assume nanoseconds
      (e.g. 43_000_000_000 ≈ 43 seconds in ns).
    - Otherwise assume already in milliseconds and return as-is.

This is intentionally simple and documented.  If the detection ever
fails for a new adapter we can add an explicit env-var override rather
than building a complex timebase registry.
"""

from __future__ import annotations

from typing import Optional

# Heuristic thresholds for auto-detection
_NS_HIGH_THRESHOLD = 1_000_000_000  # >= 10^9 → definitely ns
_NS_LOW_THRESHOLD = 10_000_000        # >= 10^7 → likely ns


def normalize_pts_to_ms(pts: Optional[int]) -> int:
    """Convert ``frame_meta.pts`` to milliseconds.

    Returns 0 if *pts* is None / 0 / falsy.
    """
    if not pts:
        return 0

    pts = int(pts)

    if pts < 0:
        return 0

    if pts < _NS_LOW_THRESHOLD:
        # Already in ms (or very small value, just pass through).
        return pts

    if pts >= _NS_HIGH_THRESHOLD:
        # Nanoseconds — divide by 1_000_000.
        return pts // 1_000_000

    # Between 10^7 and 10^9: ambiguous, but in practice Savant
    # produces timestamps in the billions for video PTS (ticks at
    # 90 kHz → 90_000 per second).  43 seconds = 3.9e9, which
    # crosses the high threshold.  A value like 43_000_000 would
    # mean 43 seconds in µs or ~12 hours in ns — both possible in
    # odd edge-cases.  We treat everything in this range as
    # microseconds and divide by 1_000.
    return pts // 1_000
