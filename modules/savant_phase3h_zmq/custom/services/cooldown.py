"""CooldownTracker — rate-limits duplicate events per key."""

from __future__ import annotations

from typing import Dict


class CooldownTracker:
    def __init__(self):
        self._cooldowns: Dict[str, int] = {}

    def can_emit(self, key: str, now_ms: int) -> bool:
        expiry = self._cooldowns.get(key)
        if expiry is None:
            return True
        return now_ms >= expiry

    def record_emit(self, key: str, cooldown_s: float, now_ms: int) -> None:
        self._cooldowns[key] = now_ms + int(cooldown_s * 1000)

    def clear(self, key: str) -> None:
        self._cooldowns.pop(key, None)

    def reset(self) -> None:
        self._cooldowns.clear()
