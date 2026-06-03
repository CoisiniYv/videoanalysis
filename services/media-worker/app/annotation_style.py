"""Annotation style policy for frontend overlay JSON.

The media worker writes structured overlay metadata only. Color and priority
decisions live here so event finalization does not scatter hardcoded colors
through annotation generation.
"""

from __future__ import annotations

from typing import Any


ALERT_RED = "#D50000"
BEHAVIOR_ORANGE = "#FF6D00"
IDENTITY_GREEN = "#00C853"
LOW_SIMILARITY_YELLOW = "#FFD600"
UNKNOWN_GRAY = "#9E9E9E"

_BEHAVIOR_EVENT_TYPES = frozenset({
    "intrusion",
    "loitering",
    "crowd_gathering",
    "running",
    "fall",
    "perimeter_breach",
})


def _fmt_similarity(similarity: float | None) -> str:
    if similarity is None:
        return ""
    return f" {similarity:.2f}"


def build_style(
    *,
    event_type: str = "",
    severity: str = "",
    identity_status: str = "unknown",
    display_name: str = "",
    similarity: float | None = None,
) -> dict[str, Any]:
    """Return the style policy output for one overlay object.

    Policy order:
    1. watchlist/live_search hit -> red, alert_hit, priority 100
    2. behavior event (intrusion, loitering, etc.) -> orange, behavior_event, priority 90
    3. identity match above threshold -> green, identity_match, priority 50
    4. low similarity candidate -> yellow, low_similarity_candidate, priority 30
    5. unknown face/person -> gray, unknown_face, priority 10
    """
    label_base = display_name.strip() if display_name else "Face"

    if event_type in ("watchlist_hit", "live_search_hit") and identity_status == "matched":
        label = f"{label_base}{_fmt_similarity(similarity)}".strip()
        return {
            "bbox_color": ALERT_RED,
            "label_color": ALERT_RED,
            "line_width": 3,
            "label": label,
            "priority": 100,
            "reason": "alert_hit",
        }

    if event_type in _BEHAVIOR_EVENT_TYPES or event_type == "behavior_event":
        label = event_type.replace("_", " ").title() if not display_name else label_base
        return {
            "bbox_color": BEHAVIOR_ORANGE,
            "label_color": BEHAVIOR_ORANGE,
            "line_width": 3,
            "label": label,
            "priority": 90,
            "reason": "behavior_event",
        }

    if identity_status == "matched":
        label = f"{label_base}{_fmt_similarity(similarity)}".strip()
        return {
            "bbox_color": IDENTITY_GREEN,
            "label_color": IDENTITY_GREEN,
            "line_width": 2,
            "label": label,
            "priority": 50,
            "reason": "identity_match",
        }

    if identity_status == "low_similarity_candidate":
        label = f"{label_base}{_fmt_similarity(similarity)}".strip()
        return {
            "bbox_color": LOW_SIMILARITY_YELLOW,
            "label_color": LOW_SIMILARITY_YELLOW,
            "line_width": 2,
            "label": label,
            "priority": 30,
            "reason": "low_similarity_candidate",
        }

    return {
        "bbox_color": UNKNOWN_GRAY,
        "label_color": UNKNOWN_GRAY,
        "line_width": 2,
        "label": label_base,
        "priority": 10,
        "reason": "unknown_face",
    }
