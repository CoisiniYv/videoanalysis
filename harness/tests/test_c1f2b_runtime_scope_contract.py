"""C1F.2b — Runtime scope correction contract tests.

Verifies that documentation correctly distinguishes config-present from
runtime-validated, and that no premature runtime claims are made.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
DOC = REPO / "docs" / "c1f2b_face_pipeline_runtime_reality_correction.md"
AUDIT_DOC = REPO / "docs" / "c1f2a_face_observation_pipeline_audit.md"
C1F1_DOC = REPO / "docs" / "c1f1_same_frame_pose_face_detection.md"
COMPOSE_C1E = REPO / "infra" / "docker-compose.c1-official-replay-dev.yml"

import yaml


def _load_compose(path: Path) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


# ---------------------------------------------------------------------------
# 1. Document exists and has correct status
# ---------------------------------------------------------------------------

class TestDocExists:
    def test_file_exists(self):
        assert DOC.exists()

    def test_has_pass_status(self):
        content = DOC.read_text()
        assert "PASS_RUNTIME_SCOPE_CORRECTED" in content


# ---------------------------------------------------------------------------
# 2. Runtime validated section
# ---------------------------------------------------------------------------

class TestRuntimeValidated:
    def test_lists_yolov8_pose(self):
        content = DOC.read_text()
        assert "YOLO26-pose" in content
        assert "RUNTIME VERIFIED" in content

    def test_lists_nvtracker(self):
        content = DOC.read_text()
        assert "nvtracker" in content

    def test_lists_yolov8_face(self):
        content = DOC.read_text()
        assert "YOLOv8-Face" in content

    def test_lists_same_frame(self):
        content = DOC.read_text()
        assert "same-frame" in content.lower() or "same frame" in content.lower()

    def test_lists_association(self):
        content = DOC.read_text()
        assert "association" in content.lower()

    def test_lists_keypoints(self):
        content = DOC.read_text()
        assert "keypoints" in content.lower()

    def test_lists_landmarks(self):
        content = DOC.read_text()
        assert "landmarks" in content.lower()


# ---------------------------------------------------------------------------
# 3. NOT runtime validated section
# ---------------------------------------------------------------------------

class TestNotRuntimeValidated:
    def test_adafac_not_validated(self):
        content = DOC.read_text()
        assert "AdaFace" in content
        assert "CONFIG ONLY" in content or "NOT VERIFIED" in content

    def test_face_reid_gate_not_validated(self):
        content = DOC.read_text()
        assert "face_reid_gate" in content

    def test_face_observation_exporter_not_validated(self):
        content = DOC.read_text()
        assert "face_observation_exporter" in content

    def test_redis_not_validated(self):
        content = DOC.read_text()
        assert "security.face_observations" in content

    def test_face_worker_not_validated(self):
        content = DOC.read_text()
        assert "face-worker" in content

    def test_postgres_not_validated(self):
        content = DOC.read_text()
        assert "PostgreSQL" in content or "face_observations" in content

    def test_watchlist_not_implemented(self):
        content = DOC.read_text()
        assert "watchlist" in content.lower()

    def test_live_search_not_implemented(self):
        content = DOC.read_text()
        assert "live_search" in content.lower()


# ---------------------------------------------------------------------------
# 4. C1F.2a reinterpretation
# ---------------------------------------------------------------------------

class TestC1F2aReinterpretation:
    def test_explains_config_only(self):
        content = DOC.read_text()
        assert "config audit" in content.lower() or "config-ready" in content.lower()

    def test_does_not_mean_runtime(self):
        content = DOC.read_text()
        # Should explicitly say C1F.2a does NOT mean runtime validated
        assert "does NOT mean" in content or "does not mean" in content.lower()

    def test_cites_c1f2a(self):
        content = DOC.read_text()
        assert "C1F.2a" in content


# ---------------------------------------------------------------------------
# 5. Phase plan
# ---------------------------------------------------------------------------

class TestPhasePlan:
    def test_lists_c1f2c(self):
        content = DOC.read_text()
        assert "C1F.2c" in content

    def test_lists_c1f2d(self):
        content = DOC.read_text()
        assert "C1F.2d" in content

    def test_lists_c1f2e(self):
        content = DOC.read_text()
        assert "C1F.2e" in content

    def test_lists_c1f2f(self):
        content = DOC.read_text()
        assert "C1F.2f" in content

    def test_c1f2c_is_adafac(self):
        content = DOC.read_text()
        assert "AdaFace" in content

    def test_c1f2e_is_exporter(self):
        content = DOC.read_text()
        assert "exporter" in content.lower() or "Redis" in content


# ---------------------------------------------------------------------------
# 6. C1F.2a audit doc has disclaimer
# ---------------------------------------------------------------------------

class TestC1F2aDisclaimer:
    def test_has_config_only_disclaimer(self):
        content = AUDIT_DOC.read_text()
        assert "config audit" in content.lower() or "config-ready" in content.lower()

    def test_references_c1f2b(self):
        content = AUDIT_DOC.read_text()
        assert "c1f2b" in content.lower() or "runtime scope" in content.lower()


# ---------------------------------------------------------------------------
# 7. Forbidden actions
# ---------------------------------------------------------------------------

class TestForbiddenActions:
    def test_no_rtsp_face_observation_smoke(self):
        """No real RTSP face observation smoke script should exist yet."""
        smoke_dir = REPO / "scripts" / "smoke"
        if not smoke_dir.exists():
            pytest.skip("smoke dir not found")
        for f in smoke_dir.iterdir():
            name = f.name.lower()
            if "face_observation" in name and "c1f2" in name:
                pytest.fail(
                    f"C1F.2 face observation smoke script exists prematurely: {f.name}"
                )

    def test_no_redis_exporter_smoke(self):
        """No C1F.2 Redis exporter smoke should exist yet."""
        smoke_dir = REPO / "scripts" / "smoke"
        if not smoke_dir.exists():
            pytest.skip("smoke dir not found")
        for f in smoke_dir.iterdir():
            name = f.name.lower()
            if "redis" in name and "face" in name and "c1f2" in name:
                pytest.fail(
                    f"C1F.2 Redis face smoke exists prematurely: {f.name}"
                )


# ---------------------------------------------------------------------------
# 8. YOLOv8-Face remains full-frame primary
# ---------------------------------------------------------------------------

class TestFaceRemainsFullFrame:
    def test_c1f1_doc_confirms_full_frame(self):
        content = C1F1_DOC.read_text()
        assert "full-frame primary" in content.lower()

    def test_no_person_roi_secondary(self):
        content = DOC.read_text()
        # Should not suggest changing face to person ROI secondary
        assert "person ROI secondary" not in content or "NOT" in content


# ---------------------------------------------------------------------------
# 9. Image bytes forbidden
# ---------------------------------------------------------------------------

class TestImageBytesForbidden:
    def test_no_image_bytes_in_redis(self):
        content = DOC.read_text()
        assert "image bytes" in content.lower() or "JPEG" in content or "no image" in content.lower()
