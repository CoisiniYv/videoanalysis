"""Configuration for the file-based evidence viewer."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    evidence_root: Path
    host: str
    port: int
    max_bundles: int


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def load_settings() -> Settings:
    return Settings(
        evidence_root=Path(os.getenv("EVIDENCE_ROOT", "/evidence")),
        host=os.getenv("EVIDENCE_VIEWER_HOST", "0.0.0.0"),
        port=_int_env("EVIDENCE_VIEWER_PORT", 8090),
        max_bundles=max(1, _int_env("EVIDENCE_VIEWER_MAX_BUNDLES", 200)),
    )
