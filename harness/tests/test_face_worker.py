"""Unit tests for face-worker persistence (pure Python, no DB)."""

import json
import math
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
FACE_WORKER_ROOT = REPO_ROOT / "services" / "face-worker"
FACE_WORKER_ROOT_STR = str(FACE_WORKER_ROOT)
if FACE_WORKER_ROOT_STR in sys.path:
    sys.path.remove(FACE_WORKER_ROOT_STR)
sys.path.insert(0, FACE_WORKER_ROOT_STR)
for _mod in [m for m in list(sys.modules) if m == "app" or m.startswith("app.")]:
    sys.modules.pop(_mod, None)

from app.config import Config, load_config
from app.redis_consumer import RedisStreamConsumer
from app.worker import (
    WatchlistMatchEmitter,
    _parse_observation,
    _process_batch,
    _validate_embedding,
)


# ── Config tests ─────────────────────────────────────────────────────────────

class TestFaceWorkerConfig:
    def test_defaults(self):
        cfg = Config(
            redis_url="redis://redis:6379/0",
            database_url="postgresql://video:video@postgres:5432/video_analytics",
            face_observation_stream="security.face_observations",
            consumer_group="face-workers",
            consumer_name="face-worker-1",
            poll_timeout_ms=5000,
            batch_size=10,
            consumer_start_id="0",
            watchlist_match_enabled=False,
            watchlist_event_stream="security.events",
            watchlist_threshold=0.50,
            watchlist_top_k=5,
            watchlist_target_external_person_ids=(),
            watchlist_target_names=(),
            watchlist_target_refresh_seconds=30,
            face_vector_backend="pgvector",
            face_vector_small_target_threshold=5,
            qdrant_url="http://qdrant:6333",
            qdrant_api_key="",
            qdrant_collection="face_gallery_current",
            qdrant_base_collection="face_gallery_adaface_512_v1",
            qdrant_prefer_grpc=True,
            qdrant_timeout_seconds=2.0,
            qdrant_search_ef=128,
            qdrant_candidate_multiplier=3,
            qdrant_min_candidates=20,
            qdrant_exact_rerank_enabled=True,
            qdrant_fallback_to_pgvector=True,
            qdrant_write_wait=True,
            qdrant_indexing_threshold_kb=1000,
            qdrant_full_scan_threshold_kb=1000,
            qdrant_default_segment_number=2,
            qdrant_hnsw_m=16,
            qdrant_hnsw_ef_construct=100,
        )
        assert cfg.face_observation_stream == "security.face_observations"
        assert cfg.consumer_group == "face-workers"
        assert cfg.poll_timeout_ms == 5000
        assert cfg.batch_size == 10
        assert cfg.consumer_start_id == "0"

    def test_load_config_defaults(self, monkeypatch):
        """Default config loads without any env vars set."""
        for var in [
            "REDIS_URL", "DATABASE_URL", "FACE_OBSERVATION_STREAM",
            "CONSUMER_GROUP", "CONSUMER_NAME", "POLL_TIMEOUT_MS", "BATCH_SIZE",
            "FACE_OBSERVATION_CONSUMER_START_ID",
        ]:
            monkeypatch.delenv(var, raising=False)
        cfg = load_config()
        assert cfg.redis_url == "redis://redis:6379/0"
        assert cfg.face_observation_stream == "security.face_observations"
        assert cfg.consumer_group == "face-workers"
        assert cfg.consumer_start_id == "0"
        assert cfg.face_vector_backend == "pgvector"
        assert cfg.qdrant_collection == "face_gallery_current"
        assert cfg.qdrant_prefer_grpc is True
        assert cfg.qdrant_indexing_threshold_kb == 1000
        assert cfg.qdrant_full_scan_threshold_kb == 1000

    def test_load_config_env_override(self, monkeypatch):
        monkeypatch.setenv("FACE_OBSERVATION_STREAM", "custom.face.stream")
        monkeypatch.setenv("CONSUMER_GROUP", "test-group")
        monkeypatch.setenv("POLL_TIMEOUT_MS", "2000")
        monkeypatch.setenv("BATCH_SIZE", "25")
        monkeypatch.setenv("FACE_OBSERVATION_CONSUMER_START_ID", "$")
        cfg = load_config()
        assert cfg.face_observation_stream == "custom.face.stream"
        assert cfg.consumer_group == "test-group"
        assert cfg.poll_timeout_ms == 2000
        assert cfg.batch_size == 25
        assert cfg.consumer_start_id == "$"

    def test_start_id_default_is_zero(self, monkeypatch):
        """Default consumer_start_id must be '0' (backfill)."""
        monkeypatch.delenv("FACE_OBSERVATION_CONSUMER_START_ID", raising=False)
        cfg = load_config()
        assert cfg.consumer_start_id == "0"

    def test_start_id_env_override_dollar(self, monkeypatch):
        """Explicit '$' for tail-only must be honored."""
        monkeypatch.setenv("FACE_OBSERVATION_CONSUMER_START_ID", "$")
        cfg = load_config()
        assert cfg.consumer_start_id == "$"


# ── Observation parsing tests ────────────────────────────────────────────────

def _make_redis_fields(data_dict: dict) -> dict:
    """Wrap a Python dict into Redis stream fields (bytes keys)."""
    data_json = json.dumps(data_dict)
    return {
        b"type": b"face_observation",
        b"source_observation_id": data_dict.get("source_observation_id", "").encode(),
        b"camera_id": data_dict.get("camera_id", "").encode(),
        b"source_id": data_dict.get("source_id", "").encode(),
        b"track_id": str(data_dict.get("track_id", "")).encode(),
        b"timestamp_ms": str(data_dict.get("timestamp_ms", "")).encode(),
        b"face_confidence": str(data_dict.get("face_confidence", "")).encode(),
        b"quality": str(data_dict.get("quality", "")).encode(),
        b"embedding_model": data_dict.get("embedding_model", "").encode(),
        b"embedding_dim": str(data_dict.get("embedding_dim", "")).encode(),
        b"data": data_json.encode(),
    }


def _make_obs_dict(**kwargs) -> dict:
    """Create a minimal valid observation dict matching midterm Redis payload."""
    defaults = {
        "schema_version": "1.0",
        "source_observation_id": "face:primary_rtsp:42:1000",
        "camera_id": "cam_midterm",
        "source_id": "primary_rtsp",
        "track_id": "42",
        "timestamp_ms": 1000,
        "frame_num": 100,
        "face_bbox": [320.0, 240.0, 60.0, 60.0],
        "person_bbox": None,
        "landmarks": [100.0, 200.0, 150.0, 200.0, 125.0, 230.0, 110.0, 240.0, 140.0, 240.0],
        "face_confidence": 0.85,
        "quality": 0.9,
        "detector_model": "yolov8_face",
        "embedding_model": "adaface",
        "embedding_dim": 512,
        "embedding": [0.01] * 512,
        "embedding_norm": 1.0,
        "model_version": None,
        "snapshot_path": None,
        "crop_path": None,
        "reid_allowed": True,
        "reid_throttle_key": "cam_midterm:primary_rtsp:42",
        "association_score": 0.93,
        "association_method": "center_inside_upper_body",
        "payload": {"camera_config_resolved": True},
    }
    defaults.update(kwargs)
    return defaults


class _FakeWatchlistCursor:
    def __init__(self, conn):
        self.conn = conn
        self.rows = []
        self.row = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, query, params=None):
        params = params or {}
        self.row = None
        if "FROM camera_rules" in query:
            self.rows = self.conn.rules_by_camera.get(params.get("camera_id"), [])
            return
        if "FROM cameras" in query:
            camera_id = self.conn.cameras_by_source.get(params.get("source_id"))
            self.row = {"id": camera_id} if camera_id else None
            self.rows = []
            return
        if "FROM persons" in query:
            person_ids = set(int(value) for value in params.get("person_ids", []))
            external_ids = set(params.get("external_ids", []))
            names = set(params.get("names", []))
            out = []
            for row in self.conn.person_rows:
                if not row.get("is_active", True):
                    continue
                matches_id = int(row["id"]) in person_ids
                matches_external = str(row.get("external_person_id") or "").lower() in external_ids
                matches_name = str(row.get("name") or "").lower() in names
                if matches_id or matches_external or matches_name:
                    out.append(row)
            self.rows = out
            return
        self.rows = []

    def fetchall(self):
        return list(self.rows)

    def fetchone(self):
        return self.row


class _FakeWatchlistConn:
    def __init__(self, *, rules_by_camera, person_rows, cameras_by_source=None):
        self.rules_by_camera = rules_by_camera
        self.person_rows = person_rows
        self.cameras_by_source = cameras_by_source or {}

    def cursor(self, *_, **__):
        return _FakeWatchlistCursor(self)


class _FakeGalleryStore:
    def __init__(self):
        self.calls = []

    def search_gallery(self, _embedding, *, top_k, min_similarity, person_ids):
        self.calls.append(
            {
                "top_k": top_k,
                "min_similarity": min_similarity,
                "person_ids": list(person_ids or []),
            }
        )
        rows = []
        for person_id in person_ids or []:
            rows.append(
                {
                    "id": person_id * 10,
                    "person_id": person_id,
                    "external_person_id": f"p{person_id}",
                    "person_name": f"Person {person_id}",
                    "similarity": 0.95,
                }
            )
        return rows


class _FakeRedis:
    def __init__(self):
        self.events = []

    def xadd(self, stream, fields, maxlen=None, approximate=True):
        self.events.append({"stream": stream, "fields": fields})
        return b"1-0"


def _make_watchlist_cfg(**overrides):
    defaults = {
        "redis_url": "redis://redis:6379/0",
        "database_url": "postgresql://video:video@postgres:5432/video_analytics",
        "face_observation_stream": "security.face_observations",
        "consumer_group": "face-workers",
        "consumer_name": "face-worker-1",
        "poll_timeout_ms": 5000,
        "batch_size": 10,
        "consumer_start_id": "0",
        "watchlist_match_enabled": True,
        "watchlist_event_stream": "security.events",
        "watchlist_threshold": 0.60,
        "watchlist_top_k": 5,
        "watchlist_target_external_person_ids": (),
        "watchlist_target_names": (),
        "watchlist_target_refresh_seconds": 30,
        "face_vector_backend": "pgvector",
        "face_vector_small_target_threshold": 5,
        "qdrant_url": "http://qdrant:6333",
        "qdrant_api_key": "",
        "qdrant_collection": "face_gallery_current",
        "qdrant_base_collection": "face_gallery_adaface_512_v1",
        "qdrant_prefer_grpc": True,
        "qdrant_timeout_seconds": 2.0,
        "qdrant_search_ef": 128,
        "qdrant_candidate_multiplier": 3,
        "qdrant_min_candidates": 20,
        "qdrant_exact_rerank_enabled": True,
        "qdrant_fallback_to_pgvector": True,
        "qdrant_write_wait": True,
        "qdrant_indexing_threshold_kb": 1000,
        "qdrant_full_scan_threshold_kb": 1000,
        "qdrant_default_segment_number": 2,
        "qdrant_hnsw_m": 16,
        "qdrant_hnsw_ef_construct": 100,
    }
    defaults.update(overrides)
    return Config(**defaults)


def _make_watchlist_emitter(cfg, conn, store, redis):
    emitter = WatchlistMatchEmitter.__new__(WatchlistMatchEmitter)
    emitter._cfg = cfg
    emitter._conn = conn
    emitter._redis = redis
    emitter._store = store
    emitter._rule_cache = {}
    emitter._camera_id_by_source_cache = {}
    emitter._env_target_person_ids = None
    emitter._last_env_target_refresh = 0.0
    return emitter


class TestObservationParsing:
    def test_valid_observation_parsed(self):
        obs_dict = _make_obs_dict()
        fields = _make_redis_fields(obs_dict)
        result = _parse_observation(fields)
        assert result is not None
        assert result["source_observation_id"] == "face:primary_rtsp:42:1000"
        assert result["camera_id"] == "cam_midterm"

    def test_missing_data_field_returns_none(self):
        fields = {
            b"type": b"face_observation",
            b"source_observation_id": b"face:primary_rtsp:42:1000",
        }
        result = _parse_observation(fields)
        assert result is None

    def test_malformed_json_returns_none(self):
        fields = {
            b"data": b"not valid json {{{",
        }
        result = _parse_observation(fields)
        assert result is None

    def test_missing_source_observation_id_returns_none(self):
        obs_dict = _make_obs_dict(source_observation_id="")
        fields = _make_redis_fields(obs_dict)
        result = _parse_observation(fields)
        assert result is None

    def test_missing_camera_id_returns_none(self):
        obs_dict = _make_obs_dict(camera_id="")
        fields = _make_redis_fields(obs_dict)
        result = _parse_observation(fields)
        assert result is None

    def test_missing_track_id_returns_none(self):
        obs_dict = _make_obs_dict(track_id="")
        fields = _make_redis_fields(obs_dict)
        result = _parse_observation(fields)
        assert result is None

    def test_embedding_preserved_as_list(self):
        feature = [0.01 * i for i in range(512)]
        obs_dict = _make_obs_dict(embedding=feature)
        fields = _make_redis_fields(obs_dict)
        result = _parse_observation(fields)
        assert len(result["embedding"]) == 512
        assert result["embedding"][0] == 0.0
        assert result["embedding"][511] == 5.11

    def test_payload_camera_config_resolved(self):
        obs_dict = _make_obs_dict(payload={"camera_config_resolved": True})
        fields = _make_redis_fields(obs_dict)
        result = _parse_observation(fields)
        assert result["payload"]["camera_config_resolved"] is True

    def test_payload_camera_config_resolved_false(self):
        obs_dict = _make_obs_dict(payload={"camera_config_resolved": False})
        fields = _make_redis_fields(obs_dict)
        result = _parse_observation(fields)
        assert result["payload"]["camera_config_resolved"] is False

    def test_all_expected_fields_present(self):
        obs_dict = _make_obs_dict()
        fields = _make_redis_fields(obs_dict)
        result = _parse_observation(fields)
        for key in [
            "source_observation_id", "camera_id", "source_id",
            "track_id", "timestamp_ms", "face_bbox", "landmarks",
            "face_confidence", "quality", "detector_model",
            "embedding_model", "embedding_dim", "embedding",
            "embedding_norm", "reid_throttle_key",
            "association_score", "association_method",
            "model_version", "snapshot_path", "crop_path",
        ]:
            assert key in result, f"Missing field: {key}"

    def test_person_bbox_may_be_null(self):
        obs_dict = _make_obs_dict(person_bbox=None)
        fields = _make_redis_fields(obs_dict)
        result = _parse_observation(fields)
        assert "person_bbox" in result
        assert result["person_bbox"] is None


class TestWatchlistCameraRules:
    def test_emitter_uses_camera_specific_watchlist_targets(self):
        conn = _FakeWatchlistConn(
            rules_by_camera={
                "cam-a": [
                    {
                        "rule_id": "rule_watchlist_a",
                        "config": {
                            "threshold": 0.81,
                            "top_k": 3,
                            "target_person_ids": [7],
                        },
                        "evidence_policy": {"pre_seconds": 2, "post_seconds": 6},
                    }
                ],
                "cam-b": [
                    {
                        "rule_id": "rule_watchlist_b",
                        "config": {
                            "threshold": 0.72,
                            "target_external_person_ids": ["p8"],
                        },
                        "evidence_policy": {},
                    }
                ],
            },
            person_rows=[
                {"id": 7, "name": "Person 7", "external_person_id": "p7", "is_active": True},
                {"id": 8, "name": "Person 8", "external_person_id": "p8", "is_active": True},
            ],
        )
        store = _FakeGalleryStore()
        redis = _FakeRedis()
        emitter = _make_watchlist_emitter(
            _make_watchlist_cfg(),
            conn,
            store,
            redis,
        )

        assert emitter.emit_for_observation(_make_obs_dict(camera_id="cam-a")) == 1
        assert emitter.emit_for_observation(_make_obs_dict(camera_id="cam-b")) == 1

        assert store.calls[0]["person_ids"] == [7]
        assert store.calls[0]["min_similarity"] == 0.81
        assert store.calls[0]["top_k"] == 3
        assert store.calls[1]["person_ids"] == [8]
        assert store.calls[1]["min_similarity"] == 0.72

        first_event = json.loads(redis.events[0]["fields"]["data"])
        assert first_event["rule_id"] == "rule_watchlist_a"
        assert first_event["evidence_policy"]["pre_seconds"] == 2
        assert first_event["payload"]["watchlist"]["match_source"] == "db_camera_rule"
        assert first_event["payload"]["watchlist"]["target_person_ids"] == [7]

    def test_emitter_resolves_source_id_camera_id_before_rule_lookup(self):
        resolved_camera_id = "11111111-1111-4111-8111-111111111111"
        source_id = "pressure60_test_24"
        conn = _FakeWatchlistConn(
            cameras_by_source={source_id: resolved_camera_id},
            rules_by_camera={
                resolved_camera_id: [
                    {
                        "rule_id": "rule_watchlist_a",
                        "config": {
                            "threshold": 0.81,
                            "target_person_ids": [7],
                        },
                        "evidence_policy": {},
                    }
                ],
            },
            person_rows=[
                {"id": 7, "name": "Person 7", "external_person_id": "p7", "is_active": True},
            ],
        )
        store = _FakeGalleryStore()
        redis = _FakeRedis()
        emitter = _make_watchlist_emitter(
            _make_watchlist_cfg(),
            conn,
            store,
            redis,
        )

        assert emitter.emit_for_observation(
            _make_obs_dict(camera_id=source_id, source_id=source_id)
        ) == 1

        first_event = json.loads(redis.events[0]["fields"]["data"])
        assert first_event["camera_id"] == resolved_camera_id
        assert first_event["source_id"] == source_id
        assert first_event["payload"]["observation"]["camera_id"] == resolved_camera_id
        assert store.calls[0]["person_ids"] == [7]

    def test_emitter_logs_gallery_query_latency(self, caplog):
        conn = _FakeWatchlistConn(
            rules_by_camera={
                "cam-a": [
                    {
                        "rule_id": "rule_watchlist_a",
                        "config": {
                            "threshold": 0.81,
                            "top_k": 3,
                            "target_person_ids": [7],
                        },
                        "evidence_policy": {},
                    }
                ],
            },
            person_rows=[
                {"id": 7, "name": "Person 7", "external_person_id": "p7", "is_active": True},
            ],
        )
        store = _FakeGalleryStore()
        redis = _FakeRedis()
        emitter = _make_watchlist_emitter(
            _make_watchlist_cfg(),
            conn,
            store,
            redis,
        )

        with caplog.at_level("INFO"):
            assert emitter.emit_for_observation(_make_obs_dict(camera_id="cam-a")) == 1

        assert "watchlist_gallery_query_completed" in caplog.text
        assert "gallery_query_duration_ms=" in caplog.text
        assert "target_count=1" in caplog.text
        assert "threshold=0.8100" in caplog.text

    def test_empty_camera_watchlist_targets_do_not_match_all_people(self):
        conn = _FakeWatchlistConn(
            rules_by_camera={
                "cam-a": [
                    {
                        "rule_id": "rule_watchlist_a",
                        "config": {
                            "threshold": 0.81,
                            "target_person_ids": [],
                            "target_external_person_ids": [],
                            "target_names": [],
                            "person_ids": [7],
                            "external_person_ids": ["p7"],
                            "names": ["Person 7"],
                        },
                        "evidence_policy": {},
                    }
                ],
            },
            person_rows=[
                {"id": 7, "name": "Person 7", "external_person_id": "p7", "is_active": True},
            ],
        )
        store = _FakeGalleryStore()
        redis = _FakeRedis()
        emitter = _make_watchlist_emitter(
            _make_watchlist_cfg(
                watchlist_target_external_person_ids=("p7",),
                watchlist_target_names=("Person 7",),
            ),
            conn,
            store,
            redis,
        )

        assert emitter.emit_for_observation(_make_obs_dict(camera_id="cam-a")) == 0
        assert store.calls == []
        assert redis.events == []


# ── Embedding validation tests — basic ───────────────────────────────────────

class TestEmbeddingValidation:
    def test_valid_embedding_passes(self):
        obs = _make_obs_dict()
        err = _validate_embedding(obs, "test-msg-1")
        assert err is None

    def test_missing_embedding_field(self):
        obs = _make_obs_dict()
        del obs["embedding"]
        err = _validate_embedding(obs, "test-msg-2")
        assert err is not None
        assert "missing" in err.lower()

    def test_embedding_is_none(self):
        obs = _make_obs_dict(embedding=None)
        err = _validate_embedding(obs, "test-msg-3")
        assert err is not None

    def test_embedding_not_a_list(self):
        obs = _make_obs_dict(embedding="not a list")
        err = _validate_embedding(obs, "test-msg-4")
        assert err is not None
        assert "not a list" in err.lower()

    def test_embedding_wrong_length(self):
        obs = _make_obs_dict(embedding=[0.01] * 256)
        err = _validate_embedding(obs, "test-msg-5")
        assert err is not None
        assert "length" in err.lower()

    def test_embedding_dim_missing(self):
        obs = _make_obs_dict()
        del obs["embedding_dim"]
        err = _validate_embedding(obs, "test-msg-6")
        assert err is not None

    def test_embedding_dim_wrong(self):
        obs = _make_obs_dict(embedding_dim=256)
        err = _validate_embedding(obs, "test-msg-7")
        assert err is not None
        assert "256" in err

    def test_embedding_norm_missing(self):
        obs = _make_obs_dict()
        del obs["embedding_norm"]
        err = _validate_embedding(obs, "test-msg-8")
        assert err is not None

    def test_embedding_norm_too_low(self):
        obs = _make_obs_dict(embedding_norm=0.5)
        err = _validate_embedding(obs, "test-msg-9")
        assert err is not None
        assert "out of range" in err.lower()

    def test_embedding_norm_too_high(self):
        obs = _make_obs_dict(embedding_norm=1.5)
        err = _validate_embedding(obs, "test-msg-10")
        assert err is not None
        assert "out of range" in err.lower()

    def test_embedding_norm_min_boundary(self):
        obs = _make_obs_dict(embedding_norm=0.90)
        err = _validate_embedding(obs, "test-msg-11")
        assert err is None

    def test_embedding_norm_max_boundary(self):
        obs = _make_obs_dict(embedding_norm=1.10)
        err = _validate_embedding(obs, "test-msg-12")
        assert err is None


# ── Embedding validation — NaN / inf / non-numeric ───────────────────────────

class TestEmbeddingFinite:
    def test_embedding_contains_nan_rejected(self):
        emb = [0.01] * 512
        emb[100] = float("nan")
        obs = _make_obs_dict(embedding=emb)
        err = _validate_embedding(obs, "test-nan")
        assert err is not None
        assert "100" in err or "invalid" in err.lower()

    def test_embedding_contains_inf_rejected(self):
        emb = [0.01] * 512
        emb[200] = float("inf")
        obs = _make_obs_dict(embedding=emb)
        err = _validate_embedding(obs, "test-inf")
        assert err is not None

    def test_embedding_contains_neg_inf_rejected(self):
        emb = [0.01] * 512
        emb[300] = float("-inf")
        obs = _make_obs_dict(embedding=emb)
        err = _validate_embedding(obs, "test-neginf")
        assert err is not None

    def test_embedding_contains_nan_string_rejected(self):
        emb = [0.01] * 512
        emb[50] = "nan"
        obs = _make_obs_dict(embedding=emb)
        err = _validate_embedding(obs, "test-str-nan")
        assert err is not None

    def test_embedding_contains_inf_string_rejected(self):
        emb = [0.01] * 512
        emb[50] = "inf"
        obs = _make_obs_dict(embedding=emb)
        err = _validate_embedding(obs, "test-str-inf")
        assert err is not None

    def test_embedding_contains_abc_string_rejected(self):
        emb = [0.01] * 512
        emb[50] = "abc"
        obs = _make_obs_dict(embedding=emb)
        err = _validate_embedding(obs, "test-str-abc")
        assert err is not None

    def test_embedding_contains_none_rejected(self):
        emb = [0.01] * 512
        emb[50] = None
        obs = _make_obs_dict(embedding=emb)
        err = _validate_embedding(obs, "test-none")
        assert err is not None

    def test_embedding_contains_bool_rejected(self):
        emb = [0.01] * 512
        emb[50] = True
        obs = _make_obs_dict(embedding=emb)
        err = _validate_embedding(obs, "test-bool")
        assert err is not None

    def test_norm_nan_rejected(self):
        obs = _make_obs_dict(embedding_norm=float("nan"))
        err = _validate_embedding(obs, "test-norm-nan")
        assert err is not None
        assert "not finite" in err.lower()

    def test_norm_inf_rejected(self):
        obs = _make_obs_dict(embedding_norm=float("inf"))
        err = _validate_embedding(obs, "test-norm-inf")
        assert err is not None
        assert "not finite" in err.lower()

    def test_valid_int_elements_accepted(self):
        emb = [1] * 512
        obs = _make_obs_dict(embedding=emb, embedding_norm=1.0)
        err = _validate_embedding(obs, "test-int")
        assert err is None

    def test_valid_mixed_int_float_accepted(self):
        emb = [0.01] * 511 + [1]
        obs = _make_obs_dict(embedding=emb, embedding_norm=1.0)
        err = _validate_embedding(obs, "test-mixed")
        assert err is None

    def test_numeric_string_rejected(self):
        """String '0.1' is rejected — only int/float literals are accepted."""
        emb = [0.01] * 512
        emb[42] = "0.1"
        obs = _make_obs_dict(embedding=emb)
        err = _validate_embedding(obs, "test-str-num")
        assert err is not None


# ── Repository mapping tests ─────────────────────────────────────────────────

class TestRepositoryMapping:
    """Verify fields map correctly to repository SQL params (no DB needed)."""

    def test_fields_extracted_correctly(self):
        obs = _make_obs_dict()
        payload = obs.get("payload", {})

        import json as _json
        def _to_jsonb(val):
            if val is None:
                return None
            if isinstance(val, str):
                return val
            return _json.dumps(val, ensure_ascii=False)

        params = {
            "source_observation_id": obs.get("source_observation_id", ""),
            "camera_id": obs.get("camera_id", ""),
            "source_id": obs.get("source_id", ""),
            "track_id": str(obs.get("track_id", "")),
            "timestamp_ms": int(obs.get("timestamp_ms", 0)),
            "captured_at": obs.get("captured_at"),
            "frame_num": obs.get("frame_num"),
            "person_bbox": _to_jsonb(obs.get("person_bbox")),
            "face_bbox": _to_jsonb(obs.get("face_bbox")),
            "landmarks": _to_jsonb(obs.get("landmarks")),
            "face_confidence": float(obs.get("face_confidence", 0.0)),
            "quality": float(obs.get("quality", 0.0)),
            "detector_model": obs.get("detector_model", "yolov8_face"),
            "embedding_model": obs.get("embedding_model", "adaface"),
            "model_version": obs.get("model_version"),
            "embedding_dim": int(obs.get("embedding_dim", 512)),
            "embedding": [float(x) for x in obs["embedding"]],
            "embedding_norm": float(obs.get("embedding_norm", 0.0)),
            "reid_throttle_key": obs.get("reid_throttle_key", ""),
            "association_score": float(obs.get("association_score", 0.0)),
            "association_method": obs.get("association_method", ""),
            "camera_config_resolved": payload.get("camera_config_resolved", False),
            "snapshot_path": obs.get("snapshot_path"),
            "crop_path": obs.get("crop_path"),
        }

        assert params["source_observation_id"] == "face:primary_rtsp:42:1000"
        assert params["camera_id"] == "cam_midterm"
        assert params["source_id"] == "primary_rtsp"
        assert params["track_id"] == "42"
        assert params["timestamp_ms"] == 1000
        assert params["embedding_dim"] == 512
        assert len(params["embedding"]) == 512
        assert params["embedding_norm"] == 1.0
        assert params["reid_throttle_key"] == "cam_midterm:primary_rtsp:42"
        assert params["camera_config_resolved"] is True
        assert params["person_bbox"] is None
        assert params["snapshot_path"] is None
        assert params["crop_path"] is None
        assert params["model_version"] is None
        assert params["captured_at"] is None

    def test_face_bbox_stored_as_jsonb(self):
        obs = _make_obs_dict(face_bbox=[100.0, 200.0, 50.0, 60.0])
        fbbox = obs.get("face_bbox")
        assert len(fbbox) == 4
        assert fbbox[0] == 100.0

    def test_landmarks_stored_as_jsonb(self):
        obs = _make_obs_dict()
        lms = obs.get("landmarks")
        assert len(lms) == 10
        assert all(isinstance(x, (int, float)) for x in lms)

    def test_embedding_not_duplicated_in_payload(self):
        obs = _make_obs_dict()
        payload = obs.get("payload", {})
        assert "embedding" not in payload

    def test_person_bbox_null_handled(self):
        obs = _make_obs_dict(person_bbox=None)
        assert obs["person_bbox"] is None

    def test_model_version_preserved(self):
        obs = _make_obs_dict(model_version="adaface_ir101_webface4m")
        assert obs["model_version"] == "adaface_ir101_webface4m"

    def test_snapshot_and_crop_path_preserved(self):
        obs = _make_obs_dict(
            snapshot_path="/media/snapshots/abc.jpg",
            crop_path="/media/crops/abc_crop.jpg",
        )
        assert obs["snapshot_path"] == "/media/snapshots/abc.jpg"
        assert obs["crop_path"] == "/media/crops/abc_crop.jpg"


# ── Invalid embedding skip behavior ──────────────────────────────────────────

class TestInvalidEmbeddingSkip:
    """Verify that invalid embedding messages are skipped but not ACKed."""

    def test_invalid_embedding_not_acked(self):
        """When embedding is invalid, the message should NOT be ACKed."""
        consumer = MagicMock()
        consumer.ack.return_value = True
        repo = MagicMock()

        obs = _make_obs_dict(embedding=[0.01] * 256)  # wrong dim
        fields = _make_redis_fields(obs)
        messages = [("msg-1", fields)]

        inserted, duplicates, skipped, failed, watchlist_emitted = _process_batch(
            messages, repo, consumer
        )

        assert inserted == 0
        assert duplicates == 0
        assert skipped == 1
        assert failed == 0
        assert watchlist_emitted == 0
        consumer.ack.assert_not_called()
        repo.insert_observation.assert_not_called()

    def test_nan_embedding_not_acked_not_inserted(self):
        """NaN embedding should be skipped, not inserted, not ACKed."""
        consumer = MagicMock()
        consumer.ack.return_value = True
        repo = MagicMock()

        emb = [0.01] * 512
        emb[0] = float("nan")
        obs = _make_obs_dict(embedding=emb)
        fields = _make_redis_fields(obs)
        messages = [("msg-nan", fields)]

        inserted, duplicates, skipped, failed, watchlist_emitted = _process_batch(
            messages, repo, consumer
        )

        assert inserted == 0
        assert duplicates == 0
        assert skipped == 1
        assert failed == 0
        assert watchlist_emitted == 0
        repo.insert_observation.assert_not_called()

    def test_valid_embedding_inserted_and_acked(self):
        consumer = MagicMock()
        consumer.ack.return_value = True
        repo = MagicMock()
        repo.insert_observation.return_value = "uuid-123"

        obs = _make_obs_dict()
        fields = _make_redis_fields(obs)
        messages = [("msg-2", fields)]

        inserted, duplicates, skipped, failed, watchlist_emitted = _process_batch(
            messages, repo, consumer
        )

        assert inserted == 1
        assert duplicates == 0
        assert skipped == 0
        assert failed == 0
        assert watchlist_emitted == 0
        consumer.ack.assert_called_once_with("msg-2")
        repo.insert_observation.assert_called_once()

    def test_unparseable_message_still_acked(self):
        consumer = MagicMock()
        consumer.ack.return_value = True
        repo = MagicMock()

        fields = {b"data": b"not valid json {{{"}
        messages = [("msg-3", fields)]

        inserted, duplicates, skipped, failed, watchlist_emitted = _process_batch(
            messages, repo, consumer
        )

        assert inserted == 0
        assert skipped == 0
        assert failed == 0
        assert watchlist_emitted == 0
        consumer.ack.assert_called_once_with("msg-3")


# ── Insert outcome classification ────────────────────────────────────────────

class TestInsertOutcome:
    """Verify that insert outcomes are correctly classified:
    inserted, duplicate, and failed are distinct.
    """

    def test_repository_duplicate_returns_duplicate_not_failed(self):
        """ON CONFLICT DO NOTHING returns None → counted as duplicate, ACKed."""
        consumer = MagicMock()
        consumer.ack.return_value = True
        repo = MagicMock()
        repo.insert_observation.return_value = None  # duplicate

        obs = _make_obs_dict()
        fields = _make_redis_fields(obs)
        messages = [("msg-dup", fields)]

        inserted, duplicates, skipped, failed, watchlist_emitted = _process_batch(
            messages, repo, consumer
        )

        assert inserted == 0
        assert duplicates == 1
        assert skipped == 0
        assert failed == 0
        assert watchlist_emitted == 0
        consumer.ack.assert_called_once_with("msg-dup")

    def test_repository_exception_counted_as_failed_not_duplicate(self):
        """DB insert exception → counted as failed, NOT duplicate, NOT ACKed."""
        consumer = MagicMock()
        consumer.ack.return_value = True
        repo = MagicMock()
        repo.insert_observation.side_effect = RuntimeError("connection lost")

        obs = _make_obs_dict()
        fields = _make_redis_fields(obs)
        messages = [("msg-fail", fields)]

        inserted, duplicates, skipped, failed, watchlist_emitted = _process_batch(
            messages, repo, consumer
        )

        assert inserted == 0
        assert duplicates == 0
        assert skipped == 0
        assert failed == 1
        assert watchlist_emitted == 0
        consumer.ack.assert_not_called()

    def test_inserted_acked(self):
        """Successful insert is ACKed."""
        consumer = MagicMock()
        consumer.ack.return_value = True
        repo = MagicMock()
        repo.insert_observation.return_value = "uuid-456"

        obs = _make_obs_dict()
        fields = _make_redis_fields(obs)
        messages = [("msg-ack", fields)]

        inserted, duplicates, skipped, failed, watchlist_emitted = _process_batch(
            messages, repo, consumer
        )

        assert inserted == 1
        assert failed == 0
        assert watchlist_emitted == 0
        consumer.ack.assert_called_once_with("msg-ack")

    def test_duplicate_acked(self):
        """Duplicate is ACKed — safe to remove from pending."""
        consumer = MagicMock()
        consumer.ack.return_value = True
        repo = MagicMock()
        repo.insert_observation.return_value = None  # duplicate

        obs = _make_obs_dict()
        fields = _make_redis_fields(obs)
        messages = [("msg-dup-ack", fields)]

        inserted, duplicates, skipped, failed, watchlist_emitted = _process_batch(
            messages, repo, consumer
        )

        assert duplicates == 1
        assert failed == 0
        assert watchlist_emitted == 0
        consumer.ack.assert_called_once_with("msg-dup-ack")

    def test_failed_not_acked(self):
        """DB exception leaves message unacked."""
        consumer = MagicMock()
        consumer.ack.return_value = True
        repo = MagicMock()
        repo.insert_observation.side_effect = RuntimeError("boom")

        obs = _make_obs_dict()
        fields = _make_redis_fields(obs)
        messages = [("msg-noack", fields)]

        inserted, duplicates, skipped, failed, watchlist_emitted = _process_batch(
            messages, repo, consumer
        )

        assert failed == 1
        assert inserted == 0
        assert duplicates == 0
        assert watchlist_emitted == 0
        consumer.ack.assert_not_called()


# ── Consumer start_id propagation ────────────────────────────────────────────

class TestConsumerStartId:
    """Verify that consumer_start_id flows from config to RedisStreamConsumer."""

    def test_start_id_passed_to_consumer(self):
        consumer = RedisStreamConsumer(
            client=MagicMock(),
            stream="test_stream",
            group="test_group",
            consumer="test_consumer",
            start_id="0",
        )
        assert consumer._start_id == "0"

    def test_start_id_dollar_for_tail_only(self):
        consumer = RedisStreamConsumer(
            client=MagicMock(),
            stream="test_stream",
            group="test_group",
            consumer="test_consumer",
            start_id="$",
        )
        assert consumer._start_id == "$"
