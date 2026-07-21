"""Tests for materialized evidence DB indexing helpers."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT.parent / "services" / "media-worker" / "app" / "evidence_db_index.py"
PHASE0_MEDIA_GOLDEN = (
    ROOT / "fixtures" / "clip_media_phase0" / "media_bundle_golden.json"
)


def _load_module():
    spec = importlib.util.spec_from_file_location("evidence_db_index", MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Transaction:
    def __enter__(self) -> "_Transaction":
        return self

    def __exit__(self, *_args: object) -> None:
        return None


class _Cursor:
    def __init__(self) -> None:
        self.executions: list[tuple[str, dict[str, Any]]] = []
        self.executemany_calls: list[tuple[str, list[dict[str, Any]]]] = []

    def __enter__(self) -> "_Cursor":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, query: str, params: dict[str, Any] | None = None) -> None:
        self.executions.append((query, params or {}))

    def executemany(self, query: str, params: list[dict[str, Any]]) -> None:
        self.executemany_calls.append((query, params))


class _Connection:
    def __init__(self) -> None:
        self.cursor_obj = _Cursor()

    def transaction(self) -> _Transaction:
        return _Transaction()

    def cursor(self) -> _Cursor:
        return self.cursor_obj


def test_upsert_overlays_merges_duplicate_clip_frame_indexes(tmp_path: Path) -> None:
    module = _load_module()
    annotations = tmp_path / "annotations.frame_cache.identity.jsonl"
    annotations.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "clip_frame_index": 7,
                        "frame_uuid": "frame-a",
                        "frame_pts": 123,
                        "t_ms": 45,
                        "objects": [{"bbox": {"xyxy": [1, 2, 3, 4]}}],
                    }
                ),
                json.dumps(
                    {
                        "clip_frame_index": 8,
                        "frame_uuid": "frame-b",
                        "objects": [],
                    }
                ),
                json.dumps(
                    {
                        "clip_frame_index": 7,
                        "frame_uuid": "frame-a-later",
                        "objects": [{"bbox": {"xyxy": [5, 6, 7, 8]}}],
                    }
                ),
            ]
        ),
        encoding="utf-8",
    )
    conn = _Connection()

    count = module._upsert_overlays(
        conn,
        event_id="11111111-1111-4111-8111-111111111111",
        path=annotations,
    )

    assert count == 2
    assert conn.cursor_obj.executemany_calls == []
    query, query_params = conn.cursor_obj.executions[0]
    params = query_params["rows"].obj
    assert "jsonb_to_recordset" in query
    assert "ON CONFLICT (event_id, clip_frame_index)" in query
    assert query_params["event_id"] == "11111111-1111-4111-8111-111111111111"
    assert [row["clip_frame_index"] for row in params] == [7, 8]
    assert params[0]["object_count"] == 2
    assert params[0]["frame_uuid"] == "frame-a-later"


def test_load_records_reads_savant_sink_metadata_frames(tmp_path: Path) -> None:
    module = _load_module()
    sink_metadata = tmp_path / "sink_metadata.json"
    sink_metadata.write_text(
        json.dumps(
            {
                "source_id": "source-a",
                "frames": [
                    {"uuid": "frame-a", "pts": 100},
                    {"uuid": "frame-b", "pts": 200},
                ],
            }
        ),
        encoding="utf-8",
    )

    records = module._load_records(sink_metadata)

    assert [record["uuid"] for record in records] == ["frame-a", "frame-b"]


def test_expanded_db_rows_do_not_publish_pruned_sidecar_artifacts(
    tmp_path: Path,
) -> None:
    module = _load_module()
    golden = json.loads(PHASE0_MEDIA_GOLDEN.read_text(encoding="utf-8"))
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "raw_clip.mov").write_bytes(
        golden["raw_clip_bytes_utf8"].encode("utf-8")
    )
    (bundle / "metadata.json").write_text(
        json.dumps({"event": {"event_id": "11111111-1111-4111-8111-111111111111"}}),
        encoding="utf-8",
    )
    (bundle / "summary.json").write_text(
        json.dumps(
            {
                "clip_status": "ready",
                "raw_clip_duration": 10.0,
                "sidecar_build_ms": 7,
            }
        ),
        encoding="utf-8",
    )
    (bundle / "summary.frame_cache.identity.json").write_text(
        json.dumps({"production_ready": True, "annotation_lines": 1}),
        encoding="utf-8",
    )
    (bundle / "annotations.frame_cache.identity.jsonl").write_text(
        json.dumps({"clip_frame_index": 0, "objects": []}) + "\n",
        encoding="utf-8",
    )
    (bundle / "sink_metadata.json").write_text(
        json.dumps({"frames": [{"uuid": "frame-1", "pts": 1}]}),
        encoding="utf-8",
    )
    conn = _Connection()

    result = module.upsert_evidence_bundle_index(
        conn,
        event_id="11111111-1111-4111-8111-111111111111",
        bundle_dir=bundle,
        compute_sha256=True,
        include_timeline=True,
        include_overlays=True,
    )

    artifact_types = [
        params.get("artifact_type")
        for query, params in conn.cursor_obj.executions
        if "INSERT INTO evidence_artifacts" in query
    ]
    assert artifact_types == golden["artifact_types"]
    assert result["artifacts"] == golden["artifact_count"]
    assert result["timeline_rows"] == golden["timeline_rows"]
    assert result["overlay_rows"] == golden["overlay_rows"]
    assert result["sidecar_build_ms"] == 7
    for field in (
        "db_bundle_index_ms",
        "db_artifact_index_ms",
        "db_timeline_index_ms",
        "db_overlay_index_ms",
        "db_index_total_ms",
    ):
        assert isinstance(result[field], int)
        assert result[field] >= 0
    assert result["db_index_total_ms"] >= result["db_bundle_index_ms"]
    bundle_params = next(
        params
        for query, params in conn.cursor_obj.executions
        if "INSERT INTO evidence_bundles" in query
    )
    indexed_summary = bundle_params["summary"].obj
    assert indexed_summary["metadata_storage"] == "db_backed"
    assert indexed_summary["timeline_storage"] == "db_backed"
    assert indexed_summary["annotation_storage"] == "db_backed"
    assert "metadata_path" not in indexed_summary
    assert "sink_metadata_path" not in indexed_summary
    assert "annotations_jsonl_path" not in indexed_summary
    assert "summary_json_path" not in indexed_summary
    assert any(
        "UPDATE events" in query and "metadata_storage" in query
        for query, _params in conn.cursor_obj.executions
    )
    assert any(
        "UPDATE evidence_tasks" in query and "metadata_path = NULL" in query
        for query, _params in conn.cursor_obj.executions
    )
    artifact_cleanup_params = next(
        params
        for query, params in conn.cursor_obj.executions
        if "DELETE FROM evidence_artifacts" in query
    )
    assert artifact_cleanup_params["artifact_types"] == [
        "overlay_annotations",
        "sink_timeline",
        "bundle_summary",
    ]
    raw_artifact_params = next(
        params
        for query, params in conn.cursor_obj.executions
        if "INSERT INTO evidence_artifacts" in query
    )
    assert raw_artifact_params["sha256"] == golden["raw_clip_sha256"]


def test_expanded_db_rows_accept_in_memory_timeline_and_overlays(
    tmp_path: Path,
) -> None:
    module = _load_module()
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "raw_clip.mov").write_bytes(b"video")
    conn = _Connection()

    result = module.upsert_evidence_bundle_index(
        conn,
        event_id="11111111-1111-4111-8111-111111111111",
        bundle_dir=bundle,
        include_timeline=True,
        include_overlays=True,
        metadata_override={"event": {"event_id": "event-1"}},
        summary_override={"clip_status": "ready", "raw_clip_duration": 10.0},
        sidecar_summary_override={
            "production_ready": True,
            "annotation_lines": 1,
        },
        timeline_rows=[
            {"clip_frame_index": 0, "uuid": "frame-1", "pts": 100}
        ],
        overlay_rows=[
            {
                "clip_frame_index": 0,
                "frame_uuid": "frame-1",
                "frame_pts": 100,
                "objects": [{"object_type": "person"}],
            }
        ],
    )

    assert result["timeline_rows"] == 1
    assert result["overlay_rows"] == 1
    queries = "\n".join(query for query, _params in conn.cursor_obj.executions)
    assert "INSERT INTO evidence_frame_timeline" in queries
    assert "INSERT INTO evidence_overlay_segments" in queries
    assert not (bundle / "sink_metadata.json").exists()
    assert not (bundle / "annotations.frame_cache.identity.jsonl").exists()
