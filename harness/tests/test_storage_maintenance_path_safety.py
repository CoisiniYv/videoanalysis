from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest


API_DIR = str(Path(__file__).resolve().parents[2] / "services" / "api")
if API_DIR not in sys.path:
    sys.path.insert(0, API_DIR)

for _mod in [m for m in list(sys.modules) if m == "app" or m.startswith("app.")]:
    sys.modules.pop(_mod, None)

from app.services.storage_maintenance import (  # noqa: E402
    MaintenanceError,
    safe_bundle_dir,
    validate_event_id_segment,
)


def test_event_id_uses_viewer_safe_character_set_without_banning_dots(tmp_path: Path) -> None:
    root = tmp_path / "evidence"
    root.mkdir()
    bundle = root / "event.v1:cam-01"
    bundle.mkdir()

    validate_event_id_segment("event.v1:cam-01")
    assert safe_bundle_dir(root, "event.v1:cam-01") == bundle.resolve(strict=False)


def test_event_id_rejects_complete_dot_segments_and_traversal(tmp_path: Path) -> None:
    root = tmp_path / "evidence"
    root.mkdir()
    for bad in ("", ".", "..", "../x", "x/y", r"x\y", "bad id", "bad%2Fid"):
        with pytest.raises(MaintenanceError):
            safe_bundle_dir(root, bad)


def test_safe_bundle_dir_rejects_symlink_escape(tmp_path: Path) -> None:
    root = tmp_path / "evidence"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    os.symlink(outside, root / "event-1")

    with pytest.raises(MaintenanceError):
        safe_bundle_dir(root, "event-1")
