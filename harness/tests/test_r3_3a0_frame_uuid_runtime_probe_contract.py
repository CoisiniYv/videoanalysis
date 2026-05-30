"""R3.3A0 frame UUID runtime probe contract checks."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
DOC = ROOT / "docs" / "r3_3a0_frame_uuid_runtime_probe.md"
PROBE = (
    ROOT
    / "modules"
    / "savant_security"
    / "custom"
    / "services"
    / "frame_uuid_probe.py"
)
BEHAVIOR_RULES = (
    ROOT
    / "modules"
    / "savant_security"
    / "custom"
    / "pyfuncs"
    / "behavior_rules.py"
)
COMPOSE = ROOT / "infra" / "docker-compose.c1-official-adapter.yml"
SMOKE = ROOT / "scripts" / "smoke" / "check_r3_3a0_frame_uuid_probe.sh"


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_probe_doc_exists_and_mentions_required_terms() -> None:
    assert DOC.exists()
    text = _text(DOC)
    for term in (
        "frame_uuid",
        "keyframe_uuid",
        "previous_keyframe_uuid",
        "VideoFrame",
        "runtime probe",
        "Replay UUID anchor",
        "Option B segment recording cannot skip drift root cause",
        "No production exact visual evidence yet",
    ):
        assert term in text


def test_doc_does_not_freeze_uuid_unavailable_as_conclusion() -> None:
    text = _text(DOC).lower()
    assert "frame_uuid must be none" not in text
    assert "uuid unavailable" not in text
    assert "must be none" not in text
    assert "do not write tests that require `frame_uuid` to be always null" in text


def test_probe_code_exists_and_is_env_gated() -> None:
    assert PROBE.exists()
    text = _text(PROBE)
    assert "R3_3A0_FRAME_UUID_PROBE_ENABLED" in text
    assert "R3_3A0_PROBE_MAX_FRAMES" in text
    assert "previous_keyframe_uuid" in text
    assert "timestamp_ms_used_by_event" in text
    assert "nested_objects" in text
    assert "video_frame_object_type" in text


def test_behavior_rules_keeps_probe_and_uses_a1_anchor_extraction() -> None:
    text = _text(BEHAVIOR_RULES)
    assert "FrameUuidRuntimeProbe" in text
    assert "self._frame_uuid_probe.probe" in text
    assert "extract_frame_anchor_metadata" in text
    assert 'event.frame_uuid = frame_anchor.get("frame_uuid")' in text
    assert 'event.keyframe_uuid = frame_anchor.get("keyframe_uuid")' in text


def test_compose_passes_probe_environment_without_module_pipeline_change() -> None:
    text = _text(COMPOSE)
    assert "R3_3A0_FRAME_UUID_PROBE_ENABLED" in text
    assert "R3_3A0_PROBE_MAX_FRAMES" in text
    assert "/data/video-analytics/media:/data/video-analytics/media:rw" in text
    module_yml = _text(ROOT / "modules" / "savant_security" / "module.yml")
    assert "frame_uuid_probe" not in module_yml
    assert "R3_3A0_FRAME_UUID_PROBE_ENABLED" not in module_yml


def test_probe_schema_with_fake_frame_meta(tmp_path: Path) -> None:
    spec = importlib.util.spec_from_file_location("frame_uuid_probe", PROBE)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    class FakeFrameMeta:
        source_id = "source-a"
        uuid = "frame-uuid-sample"
        previous_keyframe_uuid = "keyframe-uuid-sample"
        pts = 123456789
        dts = 123456000
        duration = 33333333
        frame_num = 7
        time_base = (1, 1000000000)
        metadata = {"k": "v"}

        class video_frame:
            source_id = "source-a"
            uuid = "nested-frame-uuid-sample"
            previous_keyframe_uuid = "nested-keyframe-uuid-sample"
            pts = 123456789

    probe = module.FrameUuidRuntimeProbe(
        enabled=True,
        output_root=str(tmp_path),
        max_frames=1,
    )
    sample = probe.probe(
        FakeFrameMeta(),
        timestamp_ms_used_by_event=123456,
        notes="unit schema check",
    )

    assert sample is not None
    assert sample["source_id"] == "source-a"
    assert sample["uuid"] == "frame-uuid-sample"
    assert sample["previous_keyframe_uuid"] == "keyframe-uuid-sample"
    assert sample["video_frame_object_type"] is not None
    assert sample["video_frame_uuid"] == "nested-frame-uuid-sample"
    assert sample["nested_objects"]["video_frame"]["available"] is True
    assert sample["pts"] == 123456789
    assert sample["frame_num"] == 7
    assert sample["timestamp_ms_used_by_event"] == 123456
    assert Path(sample["probe_output_path"]).exists()


def test_probe_default_disabled(monkeypatch) -> None:
    spec = importlib.util.spec_from_file_location("frame_uuid_probe_disabled", PROBE)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    monkeypatch.delenv("R3_3A0_FRAME_UUID_PROBE_ENABLED", raising=False)
    probe = module.FrameUuidRuntimeProbe()
    assert probe.enabled is False


def test_smoke_exists_and_uses_runtime_probe_env() -> None:
    assert SMOKE.exists()
    text = _text(SMOKE)
    assert "R3_3A0_FRAME_UUID_PROBE_ENABLED=true" in text
    assert "r3_3a0_frame_uuid_probe" in text
    assert "metadata-sink" in text
    assert "video_frame_object_type" in text
    assert "uuid" in text
