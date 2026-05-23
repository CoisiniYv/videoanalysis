"""API configuration from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Settings:
    database_url: str = field(
        default_factory=lambda: os.getenv(
            "DATABASE_URL",
            "postgresql://video:video@postgres:5432/video_analytics",
        )
    )
    api_title: str = "Video Analytics API"
    api_version: str = "1.0.0"


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
