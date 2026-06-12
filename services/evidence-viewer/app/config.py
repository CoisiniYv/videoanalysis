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
    operator_api_base_url: str = "http://api:8000"
    camera_config_path: Path | None = None
    sources_config_path: Path | None = None


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def load_settings() -> Settings:
    camera_config_path = _optional_path_env(
        "EVIDENCE_CAMERA_CONFIG_PATH",
        "/app/modules/savant_security/config/cameras.midterm.yml",
    )
    sources_config_path = _optional_path_env(
        "EVIDENCE_SOURCES_CONFIG_PATH",
        "/app/infra/generated/sources.generated.yml",
    )
    return Settings(
        evidence_root=Path(os.getenv("EVIDENCE_ROOT", "/evidence")),
        host=os.getenv("EVIDENCE_VIEWER_HOST", "0.0.0.0"),
        port=_int_env("EVIDENCE_VIEWER_PORT", 8090),
        max_bundles=max(1, _int_env("EVIDENCE_VIEWER_MAX_BUNDLES", 200)),
        operator_api_base_url=os.getenv(
            "OPERATOR_API_BASE_URL",
            "http://api:8000",
        ).rstrip("/"),
        camera_config_path=camera_config_path,
        sources_config_path=sources_config_path,
    )


def _optional_path_env(name: str, default: str) -> Path | None:
    raw = os.getenv(name, default)
    if raw is None or raw.strip() == "":
        return None
    return Path(raw)
