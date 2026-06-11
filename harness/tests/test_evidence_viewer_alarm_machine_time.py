from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SERVICE_ROOT = ROOT / "services" / "evidence-viewer"

if str(SERVICE_ROOT) in sys.path:
    sys.path.remove(str(SERVICE_ROOT))
sys.path.insert(0, str(SERVICE_ROOT))

for name in list(sys.modules):
    if name == "app" or name.startswith("app."):
        del sys.modules[name]

from app.evidence_index import bundle_manifest, scan_bundles  # noqa: E402


def _write_bundle(root: Path, event_id: str, event: dict) -> None:
    bundle_dir = root / event_id
    bundle_dir.mkdir(parents=True)
    (bundle_dir / "raw_clip.mp4").write_bytes(b"video")
    (bundle_dir / "metadata.json").write_text(
        json.dumps(
            {
                "event": {
                    "event_id": event_id,
                    "event_type": "intrusion",
                    "source_id": "source-1",
                    "camera_id": "camera-1",
                    **event,
                },
                "status": {"clip_status": "ready"},
            }
        ),
        encoding="utf-8",
    )
    (bundle_dir / "summary.json").write_text(
        json.dumps({"event_type": "intrusion", "source_id": "source-1"}),
        encoding="utf-8",
    )


def test_bundle_listing_and_manifest_expose_alarm_machine_time(tmp_path: Path) -> None:
    root = tmp_path / "evidence"
    _write_bundle(
        root,
        "event-1",
        {"created_at": "2026-06-11T02:05:06+00:00"},
    )

    bundle = scan_bundles(root, limit=10, offset=0)["bundles"][0]
    assert bundle["alarm_machine_time"] == "2026-06-11T02:05:06+00:00"
    assert bundle["alarm_machine_time_source"] == "event.created_at"
    assert bundle["raw_clip_url"] == "/api/bundles/event-1/media/raw_clip"

    manifest = bundle_manifest(root, "event-1")
    assert manifest["alarm_machine_time"] == "2026-06-11T02:05:06+00:00"
    assert manifest["alarm_machine_time_source"] == "event.created_at"


def test_legacy_bundle_uses_only_epoch_like_event_time(tmp_path: Path) -> None:
    root = tmp_path / "evidence"
    epoch_ms = 1_780_914_142_163
    expected = (
        datetime.fromtimestamp(epoch_ms / 1000, timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )
    _write_bundle(
        root,
        "event-1",
        {
            "event_ts_ms": 1_761_996,
            "source_event_id": f"savant_security:camera:705:intrusion:{epoch_ms}",
        },
    )

    bundle = scan_bundles(root, limit=10, offset=0)["bundles"][0]
    assert bundle["alarm_machine_time"] == expected
    assert bundle["alarm_machine_time_source"] == "event.source_event_id"
