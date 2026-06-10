#!/usr/bin/env python3
"""CLI wrapper for C2 post-Savant metadata annotation sidecar generation."""

from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
MEDIA_WORKER_ROOT = ROOT / "services" / "media-worker"
if str(MEDIA_WORKER_ROOT) not in sys.path:
    sys.path.insert(0, str(MEDIA_WORKER_ROOT))

from app.post_savant_metadata_annotation_builder import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
