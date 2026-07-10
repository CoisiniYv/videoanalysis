from __future__ import annotations

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "modules" / "savant_security" / "module.yml"
PYFUNC_PATH = ROOT / "modules" / "savant_security" / "custom" / "pyfuncs" / "savant_perf_metrics.py"
SMOKE_PATH = ROOT / "scripts" / "smoke" / "current" / "check_savant_perf_observability.sh"

REQUIRED_METRICS = [
    "va_savant_frames_seen_total",
    "va_savant_frame_annotations_exported_total",
    "va_savant_effective_fps",
    "va_savant_last_frame_age_seconds",
    "va_savant_sources_active",
    "va_savant_pose_stage_frames_total",
    "va_savant_pose_frames_with_person_total",
    "va_savant_pose_objects_total",
    "va_savant_face_stage_frames_total",
    "va_savant_face_frames_with_face_total",
    "va_savant_face_objects_total",
    "va_savant_adaface_embeddings_total",
    "va_savant_person_observations_exported_total",
    "va_savant_face_observations_exported_total",
    "va_savant_pose_stage_fps",
    "va_savant_face_stage_fps",
    "va_savant_adaface_embedding_fps",
]
REGISTERED_COUNTERS = [
    "va_savant_frames_seen",
    "va_savant_frame_annotations_exported",
    "va_savant_pose_stage_frames",
    "va_savant_pose_frames_with_person",
    "va_savant_pose_objects",
    "va_savant_face_stage_frames",
    "va_savant_face_frames_with_face",
    "va_savant_face_objects",
    "va_savant_adaface_embeddings",
    "va_savant_person_observations_exported",
    "va_savant_face_observations_exported",
]
REGISTERED_GAUGES = [
    "va_savant_effective_fps",
    "va_savant_last_frame_age_seconds",
    "va_savant_sources_active",
    "va_savant_frame_annotation_fps",
    "va_savant_pose_stage_fps",
    "va_savant_pose_object_fps",
    "va_savant_face_stage_fps",
    "va_savant_face_object_fps",
    "va_savant_adaface_embedding_fps",
    "va_savant_person_observation_fps",
    "va_savant_face_observation_fps",
]


def test_savant_perf_metrics_pyfunc_is_wired_after_frame_annotation_exporter() -> None:
    module = yaml.safe_load(MODULE_PATH.read_text(encoding="utf-8"))
    elements = module["pipeline"]["elements"]
    names = [element.get("name") for element in elements]

    assert "savant_perf_metrics" in names
    assert names.index("savant_perf_metrics") > names.index("frame_annotation_exporter")
    element = next(element for element in elements if element.get("name") == "savant_perf_metrics")
    assert element["module"] == "custom.pyfuncs.savant_perf_metrics"
    assert element["class_name"] == "SavantPerfMetricsPyFunc"
    assert "SAVANT_PERF_METRICS_ENABLED" in str(element["kwargs"]["enabled"])
    assert "SAVANT_PERF_METRICS_STAGE_RATES_ENABLED" in str(
        element["kwargs"]["stage_rates_enabled"]
    )


def test_savant_perf_metrics_exposes_stable_va_savant_names() -> None:
    text = PYFUNC_PATH.read_text(encoding="utf-8")

    for metric in REGISTERED_COUNTERS + REGISTERED_GAUGES:
        assert metric in text
    assert "_total_total" not in text
    assert '["source_id"]' in text
    assert '["source_id", "window"]' in text


def test_savant_perf_smoke_checks_required_metrics_by_source_id() -> None:
    text = SMOKE_PATH.read_text(encoding="utf-8")

    for metric in REQUIRED_METRICS:
        assert metric in text
    assert "source_id" in text
    assert "required va_savant counters did not advance" in text
