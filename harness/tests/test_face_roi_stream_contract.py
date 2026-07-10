from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import numpy as np


ROOT = Path(__file__).resolve().parents[2]


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


roi_contract = _load(
    "face_roi_stream_contract",
    ROOT / "modules/savant_security/custom/services/face_roi_stream.py",
)
observation_contract = _load(
    "adaface_roi_observation_contract",
    ROOT / "services/adaface-roi-worker/app/contracts.py",
)
runner_contract = _load(
    "adaface_roi_runner_contract",
    ROOT / "services/adaface-roi-worker/app/tensorrt_runner.py",
)


def _envelope():
    return roi_contract.FaceRoiEnvelope(
        source_id="pressure-01",
        camera_id="camera-01",
        person_track_id=42,
        face_index=1,
        timestamp_ms=1234,
        frame_num=10,
        frame_uuid="frame-uuid",
        keyframe_uuid="keyframe-uuid",
        previous_keyframe_uuid="previous-keyframe-uuid",
        frame_pts=1_234_000_000,
        frame_dts=1_230_000_000,
        duration=250_000_000,
        time_base="1/1000000000",
        ntp_timestamp=99,
        runtime_epoch_id="epoch-1",
        stream_session_id="session-1",
        face_bbox={"format": "cxcywh", "values": [10, 20, 40, 50]},
        landmarks=[1.0] * 10,
        face_confidence=0.9,
        quality=0.8,
        association_score=0.95,
        association_method="inside_person",
        throttle_key="camera-01:pressure-01:42",
        created_at_ms=5000,
        expires_at_ms=10000,
    )


def test_roi_stream_contains_only_small_image_and_identity_metadata() -> None:
    envelope = _envelope()
    fields = envelope.redis_fields(b"jpeg-small-crop")
    metadata = json.loads(fields["metadata"])

    assert fields["image"] == b"jpeg-small-crop"
    assert metadata["image_width"] == 112
    assert metadata["image_height"] == 112
    assert metadata["alignment"] == "adaface_5point_v1"
    assert metadata["source_observation_id"] == "face:pressure-01:uuid:frame-uuid:1"
    assert metadata["runtime_epoch_id"] == "epoch-1"
    assert metadata["stream_session_id"] == "session-1"
    assert "video" not in fields
    assert "h264" not in fields


def test_roi_expiry_fails_closed() -> None:
    metadata = _envelope().metadata()
    assert roi_contract.is_expired(metadata, now_ms=9999) is False
    assert roi_contract.is_expired(metadata, now_ms=10000) is True
    assert roi_contract.is_expired({}, now_ms=1) is True


def test_worker_preserves_existing_face_observation_contract() -> None:
    metadata = _envelope().metadata()
    embedding = [0.0] * 511 + [1.0]
    observation = observation_contract.build_face_observation(metadata, embedding)
    fields = observation_contract.observation_redis_fields(observation)
    payload = json.loads(fields["data"])

    assert payload["embedding_dim"] == 512
    assert payload["embedding_norm"] == 1.0
    assert payload["track_id"] == "42"
    assert payload["person_track_id"] == "42"
    assert payload["payload"]["media"]["frame_uuid"] == "frame-uuid"
    assert payload["payload"]["media"]["runtime_epoch_id"] == "epoch-1"
    assert payload["payload"]["roi_transport"]["alignment"] == "adaface_5point_v1"


def test_adaface_preprocessing_matches_savant_bgr_contract() -> None:
    image = np.zeros((112, 112, 3), dtype=np.uint8)
    image[:, :, 0] = 255
    batch = runner_contract.preprocess_bgr_batch([image])

    assert batch.shape == (1, 3, 112, 112)
    assert batch.dtype == np.float32
    assert np.isclose(batch[0, 0, 0, 0], 1.0)
    assert np.isclose(batch[0, 1, 0, 0], -1.0)
    assert np.isclose(batch[0, 2, 0, 0], -1.0)
    assert batch.flags.c_contiguous


def test_roi_exporter_batches_gpu_sync_once_per_frame() -> None:
    source = (
        ROOT
        / "modules/savant_security/custom/pyfuncs/face_roi_exporter.py"
    ).read_text(encoding="utf-8")

    assert source.count("self._cuda_stream.waitForCompletion()") == 1
    assert source.index("self._cuda_stream.waitForCompletion()") < source.index(
        "for face_index, obj, inp, verdict, aligned in aligned_faces:"
    )
    assert '"gpu_syncs": 0' in source
    assert '"max_eligible_per_frame": 0' in source
    assert "queue.Queue(" in source
    assert 'name="face-roi-download-encoder"' in source
    assert source.index("def _crop_worker") < source.index("aligned.to_cpu()")


def test_roi_worker_overrides_inherited_savant_healthcheck() -> None:
    dockerfile = (
        ROOT / "services/adaface-roi-worker/Dockerfile"
    ).read_text(encoding="utf-8")

    assert "HEALTHCHECK" in dockerfile
    assert "http://127.0.0.1:8080/metrics" in dockerfile
