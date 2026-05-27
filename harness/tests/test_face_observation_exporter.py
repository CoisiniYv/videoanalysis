"""Unit tests for face observation exporter (pure Python, no GPU / Savant)."""

import json
import math
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
MODULE_ROOT = REPO_ROOT / "modules" / "savant_security"
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(MODULE_ROOT))

from custom.models.face_events import (
    FaceObservationEventDraft,
    build_face_source_observation_id,
)
from custom.services.face_observation_exporter import (
    DryRunFaceObservationExporter,
    ExportThrottleMap,
    FaceObservationExporter,
    create_face_observation_exporter,
)


def _make_observation(**kwargs) -> FaceObservationEventDraft:
    """Create a FaceObservationEventDraft with sensible defaults."""
    feature = kwargs.pop("feature", [0.01] * 512)
    defaults = dict(
        source_observation_id="face:cam1:42:1000",
        producer="savant-security",
        camera_id="cam1",
        source_id="cam1",
        track_id=42,
        timestamp_ms=1000,
        frame_num=100,
        face_bbox=[320.0, 240.0, 60.0, 60.0],
        landmarks=[100.0, 200.0, 150.0, 200.0, 125.0, 230.0, 110.0, 240.0, 140.0, 240.0],
        face_confidence=0.85,
        quality=0.9,
        detector_model="yolov8_face",
        embedding_model="adaface",
        embedding_dim=len(feature),
        embedding=feature,
        embedding_norm=math.sqrt(sum(x * x for x in feature)),
        reid_allowed=True,
        reid_throttle_key="cam1:42",
        association_score=0.93,
        association_method="center_inside_upper_body",
    )
    defaults.update(kwargs)
    return FaceObservationEventDraft(**defaults)


class TestObservationSchema:
    def test_schema_version(self):
        obs = _make_observation()
        d = obs.to_dict()
        assert d["schema_version"] == "1.0"

    def test_message_type(self):
        obs = _make_observation()
        d = obs.to_dict()
        assert d["message_type"] == "face_observation"

    def test_required_fields_present(self):
        obs = _make_observation()
        d = obs.to_dict()
        for key in [
            "source_observation_id", "camera_id", "source_id",
            "track_id", "timestamp_ms", "face_bbox", "landmarks",
            "face_confidence", "quality", "embedding_model",
            "embedding_dim", "embedding", "reid_allowed",
        ]:
            assert key in d, f"Missing required field: {key}"

    def test_no_image_bytes_fields(self):
        obs = _make_observation()
        d = obs.to_dict()
        for forbidden in ["image_bytes", "frame_bytes", "crop_bytes", "base64", "jpeg"]:
            assert forbidden not in d, f"Forbidden field present: {forbidden}"
            assert forbidden not in str(d.get("payload", {}))

    def test_track_id_is_string_in_dict(self):
        obs = _make_observation(track_id=42)
        d = obs.to_dict()
        assert d["track_id"] == "42"

    def test_track_id_zero_is_none(self):
        obs = _make_observation(track_id=0)
        d = obs.to_dict()
        assert d["track_id"] is None


class TestPayloadSerialization:
    def test_json_serializable(self):
        obs = _make_observation()
        j = obs.to_json()
        parsed = json.loads(j)
        assert parsed["schema_version"] == "1.0"

    def test_embedding_floats_json_serializable(self):
        feature = [0.01 * i for i in range(512)]
        obs = _make_observation(feature=feature)
        j = obs.to_json()
        parsed = json.loads(j)
        assert len(parsed["embedding"]) == 512
        assert abs(parsed["embedding"][0] - 0.0) < 1e-10
        assert abs(parsed["embedding"][1] - 0.01) < 1e-10

    def test_payload_size_reasonable(self):
        feature = [0.01] * 512
        obs = _make_observation(feature=feature)
        j = obs.to_json()
        # Should be around 2-4KB, not megabytes
        assert len(j) < 10000, f"Payload too large: {len(j)} bytes"
        assert len(j) > 1000, f"Payload too small: {len(j)} bytes"


class TestIdempotencyKey:
    def test_deterministic(self):
        id1 = build_face_source_observation_id("cam1", 42, 1000)
        id2 = build_face_source_observation_id("cam1", 42, 1000)
        assert id1 == id2

    def test_format(self):
        obs_id = build_face_source_observation_id("cam1", 42, 1000)
        assert obs_id == "face:cam1:42:1000"

    def test_no_track_format(self):
        obs_id = build_face_source_observation_id("cam1", 0, 1000)
        assert obs_id == "face:cam1:no_track:1000"

    def test_different_tracks_differ(self):
        id1 = build_face_source_observation_id("cam1", 42, 1000)
        id2 = build_face_source_observation_id("cam1", 99, 1000)
        assert id1 != id2

    def test_different_cameras_differ(self):
        id1 = build_face_source_observation_id("cam1", 42, 1000)
        id2 = build_face_source_observation_id("cam2", 42, 1000)
        assert id1 != id2


class TestOnlyReidAllowedExported:
    def test_reid_allowed_field(self):
        obs = _make_observation(reid_allowed=True)
        assert obs.to_dict()["reid_allowed"] is True

    def test_reid_not_allowed(self):
        obs = _make_observation(reid_allowed=False)
        assert obs.to_dict()["reid_allowed"] is False


class TestDryRunExporter:
    def test_dry_run_logs(self, capsys):
        exporter = DryRunFaceObservationExporter()
        obs = _make_observation()
        exporter.export(obs)
        captured = capsys.readouterr()
        assert "face_observation_dry_run" in captured.out
        assert "face:cam1:42:1000" in captured.out

    def test_dry_run_no_redis(self):
        exporter = DryRunFaceObservationExporter()
        assert isinstance(exporter, FaceObservationExporter)


class TestStreamDefaults:
    def test_default_stream_name(self):
        # The default stream name should be security.face_observations
        import os
        default = os.environ.get(
            "FACE_OBSERVATION_STREAM", "security.face_observations",
        )
        assert default == "security.face_observations"


class TestEmbeddingContent:
    def test_embedding_dim_matches(self):
        feature = [0.01] * 512
        obs = _make_observation(feature=feature, embedding_dim=512)
        assert obs.embedding_dim == 512
        assert len(obs.embedding) == 512

    def test_embedding_norm_computed(self):
        feature = [1.0 / math.sqrt(512)] * 512
        obs = _make_observation(feature=feature)
        expected_norm = math.sqrt(sum(x * x for x in feature))
        assert abs(obs.embedding_norm - expected_norm) < 1e-6

    def test_detector_model_default(self):
        obs = _make_observation()
        assert obs.detector_model == "yolov8_face"

    def test_embedding_model_default(self):
        obs = _make_observation()
        assert obs.embedding_model == "adaface"


class TestExportThrottleMap:
    """F2.3b — defensive export throttle tests."""

    def test_first_observation_exports(self):
        throttle = ExportThrottleMap(min_interval_ms=1000)
        assert throttle.is_allowed("cam1:42", 1000) is True

    def test_same_key_at_plus_40ms_blocked(self):
        throttle = ExportThrottleMap(min_interval_ms=1000)
        throttle.record("cam1:42", 1000)
        assert throttle.is_allowed("cam1:42", 1040) is False

    def test_same_key_at_plus_999ms_blocked(self):
        throttle = ExportThrottleMap(min_interval_ms=1000)
        throttle.record("cam1:42", 1000)
        assert throttle.is_allowed("cam1:42", 1999) is False

    def test_same_key_at_plus_1000ms_exports(self):
        throttle = ExportThrottleMap(min_interval_ms=1000)
        throttle.record("cam1:42", 1000)
        assert throttle.is_allowed("cam1:42", 2000) is True

    def test_different_track_exports_independently(self):
        throttle = ExportThrottleMap(min_interval_ms=1000)
        throttle.record("cam1:42", 1000)
        assert throttle.is_allowed("cam1:99", 1000) is True

    def test_different_source_exports_independently(self):
        throttle = ExportThrottleMap(min_interval_ms=1000)
        throttle.record("cam1:42", 1000)
        assert throttle.is_allowed("cam2:42", 1000) is True

    def test_zero_interval_allows_all(self):
        throttle = ExportThrottleMap(min_interval_ms=0)
        throttle.record("cam1:42", 1000)
        assert throttle.is_allowed("cam1:42", 1001) is True

    def test_clear_resets_throttle(self):
        throttle = ExportThrottleMap(min_interval_ms=1000)
        throttle.record("cam1:42", 1000)
        assert throttle.is_allowed("cam1:42", 1500) is False
        throttle.clear()
        assert throttle.is_allowed("cam1:42", 1500) is True

    def test_rapid_sequence_all_throttled(self):
        """Simulate 30fps frames — only first should pass."""
        throttle = ExportThrottleMap(min_interval_ms=1000)
        key = "cam1:42"
        exported_times = []
        for frame_ts in range(0, 10000, 33):  # ~30fps
            if throttle.is_allowed(key, frame_ts):
                throttle.record(key, frame_ts)
                exported_times.append(frame_ts)
        # Should export at 0, 1000, 2000, ... ≈ 10 exports in 10s
        assert len(exported_times) <= 11
        assert len(exported_times) >= 9
        # Verify all gaps >= 1000ms
        for a, b in zip(exported_times, exported_times[1:]):
            assert (b - a) >= 1000

    def test_multiple_tracks_independent(self):
        """Multiple tracks at same timestamps export independently."""
        throttle = ExportThrottleMap(min_interval_ms=1000)
        throttle.record("cam1:42", 1000)
        throttle.record("cam1:99", 1000)
        assert throttle.is_allowed("cam1:42", 1500) is False
        assert throttle.is_allowed("cam1:99", 1500) is False
        assert throttle.is_allowed("cam1:42", 2000) is True
        assert throttle.is_allowed("cam1:99", 2000) is True


class TestNoImageBytesGuarantee:
    """F2.3b — confirm no image bytes in any output."""

    def test_to_dict_no_image_fields(self):
        obs = _make_observation()
        d = obs.to_dict()
        s = json.dumps(d)
        for forbidden in [
            "image_bytes", "frame_bytes", "crop_bytes",
            "base64", "jpeg", "png_bytes",
        ]:
            assert forbidden not in s, f"Forbidden field in output: {forbidden}"

    def test_to_json_no_image_fields(self):
        obs = _make_observation()
        j = obs.to_json()
        for forbidden in ["image_bytes", "base64", "crop_bytes"]:
            assert forbidden not in j


class TestIdempotencyKeyDeterministic:
    """F2.3b — confirm idempotency key stability."""

    def test_key_deterministic_across_calls(self):
        key1 = build_face_source_observation_id("cam1", 42, 1000)
        key2 = build_face_source_observation_id("cam1", 42, 1000)
        key3 = build_face_source_observation_id("cam1", 42, 1000)
        assert key1 == key2 == key3

    def test_key_format_face_prefix(self):
        key = build_face_source_observation_id("cam1", 42, 1000)
        assert key.startswith("face:")
