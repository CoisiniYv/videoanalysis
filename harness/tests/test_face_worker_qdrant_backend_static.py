"""Static deployment contract for Qdrant face gallery rollout."""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
FACE_WORKER_ROOT = ROOT / "services" / "face-worker"
FACE_WORKER_ROOT_STR = str(FACE_WORKER_ROOT)
if FACE_WORKER_ROOT_STR in sys.path:
    sys.path.remove(FACE_WORKER_ROOT_STR)
sys.path.insert(0, FACE_WORKER_ROOT_STR)
for _mod in [m for m in list(sys.modules) if m == "app" or m.startswith("app.")]:
    sys.modules.pop(_mod, None)

from app.config import load_config
from app.face_match_event_service import build_watchlist_hit_event


def _env_file() -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in (ROOT / "infra" / "env" / "midterm.env").read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key] = value
    return values


def test_default_backend_remains_pgvector(monkeypatch):
    for key in (
        "FACE_VECTOR_BACKEND",
        "QDRANT_URL",
        "QDRANT_COLLECTION",
        "QDRANT_FALLBACK_TO_PGVECTOR",
    ):
        monkeypatch.delenv(key, raising=False)

    cfg = load_config()
    assert cfg.face_vector_backend == "pgvector"
    assert cfg.qdrant_url == "http://qdrant:6333"
    assert cfg.qdrant_collection == "face_gallery_current"
    assert cfg.qdrant_base_collection == "face_gallery_adaface_512_v1"
    assert cfg.qdrant_prefer_grpc is True
    assert cfg.qdrant_fallback_to_pgvector is True
    assert cfg.qdrant_indexing_threshold_kb == 1000
    assert cfg.qdrant_full_scan_threshold_kb == 1000
    assert cfg.qdrant_default_segment_number == 2


def test_midterm_compose_adds_qdrant_inert_profile_and_face_worker_env():
    compose = yaml.safe_load((ROOT / "infra" / "docker-compose.midterm.yml").read_text(encoding="utf-8"))
    qdrant = compose["services"]["qdrant"]
    face_env = compose["services"]["face-worker"]["environment"]
    env_file = _env_file()

    assert qdrant["image"] == "qdrant/qdrant:v1.18.2"
    assert qdrant["profiles"] == ["qdrant"]
    assert "ports" not in qdrant
    assert "/data/video-analytics/qdrant-midterm:/qdrant/storage" in qdrant["volumes"]

    assert env_file["FACE_VECTOR_BACKEND"] == "pgvector"
    assert face_env["FACE_VECTOR_BACKEND"] == "${FACE_VECTOR_BACKEND:-pgvector}"
    assert face_env["QDRANT_COLLECTION"] == "${QDRANT_COLLECTION:-face_gallery_current}"
    assert face_env["QDRANT_PREFER_GRPC"] == "${QDRANT_PREFER_GRPC:-true}"
    assert face_env["QDRANT_FALLBACK_TO_PGVECTOR"] == "${QDRANT_FALLBACK_TO_PGVECTOR:-true}"
    assert env_file["QDRANT_PREFER_GRPC"] == "true"
    assert env_file["QDRANT_INDEXING_THRESHOLD_KB"] == "1000"
    assert env_file["QDRANT_FULL_SCAN_THRESHOLD_KB"] == "1000"
    assert face_env["QDRANT_INDEXING_THRESHOLD_KB"] == "${QDRANT_INDEXING_THRESHOLD_KB:-1000}"
    assert face_env["QDRANT_FULL_SCAN_THRESHOLD_KB"] == "${QDRANT_FULL_SCAN_THRESHOLD_KB:-1000}"


def test_watchlist_hit_payload_accepts_qdrant_result_shape_without_schema_change():
    observation = {
        "source_observation_id": "face:cam:1:1000",
        "camera_id": "cam",
        "source_id": "src",
        "track_id": "1",
        "timestamp_ms": 1000,
        "face_bbox": [1, 2, 3, 4],
        "landmarks": [],
        "quality": 0.8,
        "face_confidence": 0.9,
        "payload": {"media": {"frame_uuid": "frame-1"}},
    }
    qdrant_match = {
        "id": 55,
        "person_id": 7,
        "person_name": "Reese",
        "external_person_id": "demo:midterm:reese",
        "source_type": "manual_upload",
        "embedding_model": "adaface",
        "is_primary": True,
        "quality": 0.9,
        "similarity": 0.88,
        "distance": 0.12,
        "created_at": "2026-06-29T00:00:00Z",
    }
    event = build_watchlist_hit_event(
        observation=observation,
        gallery_match=qdrant_match,
        threshold=0.60,
        rule_id="rule",
        target_person_ids=[7],
    )

    assert event["event_type"] == "watchlist_hit"
    assert event["source_event_id"] == "watchlist_hit:face:cam:1:1000:7"
    assert event["payload"]["match"]["gallery_embedding_id"] == 55
    assert event["payload"]["match"]["similarity"] == 0.88


def test_qdrant_scale_benchmark_uses_temporary_collection_and_target_filters():
    script = (FACE_WORKER_ROOT / "benchmark_qdrant_gallery_scale.py").read_text(
        encoding="utf-8"
    )

    assert "face_gallery_scale_bench_" in script
    assert "delete_collection" in script
    assert "person_id" in script
    assert "MatchAny" in script
    assert "target-sizes" in script
    assert "with_vectors=False" in script
    assert "prefer-grpc" in script
    assert "indexing-threshold-kb" in script
    assert "max-p95-ms" in script
