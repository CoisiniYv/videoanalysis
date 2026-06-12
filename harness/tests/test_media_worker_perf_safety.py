"""Media-worker performance safety tests for midterm runtime."""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = ROOT.parent
EVENT_ID = "11111111-1111-4111-8111-111111111111"
CURRENT_EPOCH = "midterm-20260612T010203Z-a1b2c3d4"
OLD_EPOCH = "midterm-20260612T000000Z-00aa11bb"


def _activate(service: str, module_name: str):
    service_root = str(REPO_ROOT / "services" / service)
    for name in list(sys.modules):
        if name == "app" or name.startswith("app."):
            del sys.modules[name]
    if service_root in sys.path:
        sys.path.remove(service_root)
    sys.path.insert(0, service_root)
    return importlib.import_module(module_name)


def test_processed_sink_dirs_survive_restart(tmp_path: Path) -> None:
    worker = _activate("media-worker", "app.worker")
    root = tmp_path / "replay-sink-output" / "midterm" / "epochs" / CURRENT_EPOCH
    sink_dir = root / f"replay-event-{EVENT_ID}-00000000"
    sink_dir.mkdir(parents=True)
    (sink_dir / "metadata.json").write_text(
        json.dumps({"labels": {"event_id": EVENT_ID}, "source_id": "source-1"})
        + "\n",
        encoding="utf-8",
    )
    (sink_dir / "clip.mov").write_bytes(b"video")

    state_path = tmp_path / "media-worker-state.json"
    worker._save_processed_sink_state(state_path, {str(sink_dir)})
    processed_dirs: set[str] = set()

    updated = worker._process_sink_output(
        object(),
        str(root),
        processed_dirs,
        processed_state_path=state_path,
    )

    assert updated == 0
    assert processed_dirs == {str(sink_dir)}


def test_active_epoch_scan_uses_incremental_children_and_ignores_old_epoch(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    worker = _activate("media-worker", "app.worker")
    root = tmp_path / "replay-sink-output" / "midterm"
    state_path = root / ".current_epoch.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(
        json.dumps({"runtime_epoch_id": CURRENT_EPOCH}) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("RUNTIME_EPOCH_STATE_PATH", str(state_path))

    current_dir = root / "epochs" / CURRENT_EPOCH / f"replay-event-{EVENT_ID}"
    old_dir = root / "epochs" / OLD_EPOCH / f"replay-event-{EVENT_ID}"
    for path, source_id in ((current_dir, "current-source"), (old_dir, "old-source")):
        path.mkdir(parents=True)
        (path / "metadata.json").write_text(
            json.dumps({"labels": {"event_id": EVENT_ID}, "source_id": source_id})
            + "\n",
            encoding="utf-8",
        )

    active_root = worker._active_epoch_sink_output_dir(str(root))
    rows, stats = worker._scan_metadata_files(active_root, processed_dirs=set())

    assert [row["source_id"] for row in rows] == ["current-source"]
    assert stats["scan_mode"] == "active_epoch_incremental"
    assert stats["rglob_fallback_used"] is False
    assert stats["metadata_files_visited"] == 1


def test_frame_cache_reader_uses_bounded_stream_range_and_filters_identity() -> None:
    writer = _activate("media-worker", "app.frame_cache_sidecar_writer")

    class FakeRedis:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        def xrevrange(
            self,
            name: str,
            max: str = "+",
            min: str = "-",
            count: int | None = None,
        ) -> list[tuple[str, dict[str, str]]]:
            self.calls.append({"name": name, "max": max, "min": min, "count": count})
            return [
                (
                    "1781197210000-0",
                    {"data": json.dumps(_frame_annotation("wrong-session", stream_session_id="old-session"))},
                ),
                (
                    "1781197209000-0",
                    {"data": json.dumps(_frame_annotation("wrong-source", source_id="source-2"))},
                ),
                (
                    "1781197208000-0",
                    {"data": json.dumps(_frame_annotation("wrong-camera", camera_id="camera-2"))},
                ),
                (
                    "1781197207000-0",
                    {"data": json.dumps(_frame_annotation("current-frame"))},
                ),
            ]

    redis = FakeRedis()
    messages, summary = writer._read_frame_annotations(
        redis_client=redis,
        config={
            "stream_name": "security.frame_annotations",
            "lookback_count": 20000,
            "range_count": 5,
            "max_scan": 20000,
            "pre_seconds": 5,
            "post_seconds": 5,
        },
        event={
            "event_id": EVENT_ID,
            "event_type": "intrusion",
            "created_at": "2026-06-12T01:00:00Z",
            "source_id": "source-1",
            "camera_id": "camera-1",
            "frame_uuid": "current-frame",
            "frame_pts": 100_000_000_000,
            "payload": {
                "runtime_epoch_id": CURRENT_EPOCH,
                "stream_session_id": "session-1",
            },
        },
    )

    assert [message["frame_uuid"] for message in messages] == ["current-frame"]
    assert redis.calls == [
        {
            "name": "security.frame_annotations",
            "max": summary["range_max"],
            "min": summary["range_min"],
            "count": 5,
        }
    ]
    assert summary["bounded_range_used"] is True
    assert summary["read_mode"] == "bounded_stream_id_range"
    assert summary["range_max"] != "+"
    assert summary["range_min"] != "-"
    assert summary["messages_filtered_stream_session"] == 1
    assert summary["messages_filtered_source"] == 1
    assert summary["messages_filtered_camera"] == 1
    assert summary["messages_retained"] == 1


def _frame_annotation(
    frame_uuid: str,
    *,
    runtime_epoch_id: str = CURRENT_EPOCH,
    stream_session_id: str = "session-1",
    source_id: str = "source-1",
    camera_id: str = "camera-1",
) -> dict[str, object]:
    return {
        "message_type": "frame_annotation",
        "source_id": source_id,
        "camera_id": camera_id,
        "frame_uuid": frame_uuid,
        "frame_pts": 100_000_000_000,
        "timestamp_ms": 1781197200000,
        "runtime_epoch_id": runtime_epoch_id,
        "stream_session_id": stream_session_id,
        "objects": [],
    }
