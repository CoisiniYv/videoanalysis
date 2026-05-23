"""CooldownTracker — rate-limits duplicate events per key."""

from __future__ import annotations

from typing import Dict


class CooldownTracker:
    """Prevents re-emitting events for the same key within a cooldown window.

    Keys are typically ``"<track_id>:<zone>"``.
    """

    def __init__(self):
        self._cooldowns: Dict[str, int] = {}

    def can_emit(self, key: str, now_ms: int) -> bool:
        """Check whether *key* is allowed to emit an event at *now_ms*."""
        expiry = self._cooldowns.get(key)
        if expiry is None:
            return True
        return now_ms >= expiry

    def record_emit(self, key: str, cooldown_s: float, now_ms: int) -> None:
        """Mark *key* as having emitted; next allowed time is *now_ms + cooldown_s*."""
        self._cooldowns[key] = now_ms + int(cooldown_s * 1000)

    def clear(self, key: str) -> None:
        self._cooldowns.pop(key, None)

    def reset(self) -> None:
        self._cooldowns.clear()
