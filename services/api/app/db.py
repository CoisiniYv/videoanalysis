"""Database connection helper."""

from __future__ import annotations

import psycopg
from psycopg.rows import dict_row

from app.config import get_settings


def get_conn() -> psycopg.Connection:
    """Return a new sync psycopg connection with dict_row factory."""
    settings = get_settings()
    return psycopg.connect(
        settings.database_url,
        row_factory=dict_row,
        autocommit=True,
    )
