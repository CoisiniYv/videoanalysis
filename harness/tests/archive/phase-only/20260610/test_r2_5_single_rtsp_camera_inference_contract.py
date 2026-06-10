"""Static contract checks for R2.5 single RTSP camera inference."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
DOC = ROOT / "docs" / "operator_run_single_rtsp_camera_inference.md"
SCRIPT = ROOT / "scripts" / "smoke" / "check_r2_5_single_rtsp_camera_inference.sh"


def _read(path: Path) -> str:
    assert path.exists(), f"missing file: {path}"
    return path.read_text(encoding="utf-8")


def test_required_files_exist() -> None:
    assert DOC.exists()
    assert SCRIPT.exists()


def test_doc_describes_required_pipeline_flow() -> None:
    text = _read(DOC)
    assert "RTSP -> source adapter -> Savant -> Redis -> DB" in text
    assert "YOLO26-pose" in text
    assert "YOLOv8-Face full-frame primary" in text
    assert "AdaFace" in text
    assert "ROI" in text
    assert "polygon" in text
    assert "camera_source_controller.py" in text


def test_script_supports_required_environment() -> None:
    text = _read(SCRIPT)
    for token in [
        "R2_5_RTSP_URL",
        "R2_5_CAMERA_ID",
        "R2_5_SOURCE_ID",
        "R2_5_CAMERA_NAME",
        "R2_5_WAIT_SECONDS",
        "R2_5_MIN_FACE_OBSERVATIONS",
        "R2_5_MIN_EVENTS",
        "R2_5_ROI_POLYGON",
        "DATABASE_URL",
    ]:
        assert token in text


def test_script_queries_face_observation_outputs() -> None:
    text = _read(SCRIPT)
    assert "security.face_observations" in text
    assert "face_observations" in text
    assert "embedding_dim = 512" in text
    assert "embedding_model = 'adaface'" in text
    assert "embedding_norm BETWEEN 0.90 AND 1.10" in text


def test_script_checks_events_without_requiring_them_by_default() -> None:
    text = _read(SCRIPT)
    assert "security.events" in text
    assert "FROM events" in text
    assert "R2_5_MIN_EVENTS=0" in text
    assert "events are optional" in text


def test_script_uses_rtsp_source_adapter_path() -> None:
    text = _read(SCRIPT)
    assert "camera_source_controller.py" in text
    assert "rtsp://" in text
    assert "rtsps://" in text
    assert "dealer+connect:tcp://savant-security:5555" in text
    assert "c1-official-adapter_default" in text


def test_script_does_not_use_local_video_source_adapter() -> None:
    text = _read(SCRIPT)
    assert "file://" not in text
    assert "video_loop.sh" not in text
    assert "--testvideo-mount" not in text


def test_script_does_not_modify_pipeline_yaml() -> None:
    text = _read(SCRIPT)
    assert "module.yml" not in text
    assert "sed -i" not in text


def test_script_is_not_a_performance_test() -> None:
    text = _read(SCRIPT).lower()
    assert "performance test" in text
    assert "max_parallel_streams" not in text
    assert "batch matrix" not in text
    assert "16 streams" not in text
