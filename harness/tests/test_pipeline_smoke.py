"""Phase 1C — pipeline smoke test.

Verifies that the Phase 1C pipeline definition is structurally complete.
GPU-dependent checks (actual container run) are gated behind the 'gpu' marker.

Run without GPU:
    pytest harness/tests/test_pipeline_smoke.py -q

Run full GPU smoke test:
    pytest harness/tests/test_pipeline_smoke.py -q --gpu
"""

import os
import subprocess
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
COMPOSE_FILE = REPO_ROOT / "infra" / "docker-compose.phase1c.yml"
TEST_VIDEO = REPO_ROOT / "testVideo" / "test.mp4"
MODULE_YAML = REPO_ROOT / "modules" / "savant_security" / "module.yml"
FRAME_PROBE = (
    REPO_ROOT
    / "modules"
    / "savant_security"
    / "custom"
    / "pyfuncs"
    / "minimal_frame_probe.py"
)
CAMERAS_YAML = REPO_ROOT / "modules" / "savant_security" / "config" / "cameras.yml"

COMPOSE_PROJECT = "phase1c"
SAVANT_SERVICE = "savant-phase1c"
REDIS_SERVICE = "redis"
RTSP_SERVICE = "rtsp-server"
FFMPEG_SERVICE = "phase1c-ffmpeg-source"
DC_CMD = os.environ.get("DC", "sudo docker compose -f infra/docker-compose.phase1c.yml")


def _dc(*args: str) -> subprocess.CompletedProcess:
    full_cmd = DC_CMD.split() + list(args)
    return subprocess.run(full_cmd, capture_output=True, text=True, cwd=REPO_ROOT)


# ------------------------------------------------------------------
# Structural checks (no GPU / Docker required)
# ------------------------------------------------------------------


class TestPipelineStructure:
    """Verification that all required files and configurations exist."""

    def test_compose_file_exists(self):
        assert COMPOSE_FILE.is_file(), f"Missing: {COMPOSE_FILE}"

    def test_test_video_exists(self):
        assert TEST_VIDEO.is_file(), f"Missing: {TEST_VIDEO}"
        assert TEST_VIDEO.stat().st_size > 0, f"Empty: {TEST_VIDEO}"

    def test_module_yml_exists(self):
        assert MODULE_YAML.is_file(), f"Missing: {MODULE_YAML}"
        content = MODULE_YAML.read_text()
        assert "source_uri" in content, "module.yml missing source_uri parameter"
        assert "frame_probe" in content, "module.yml missing frame_probe element"
        assert "pyfunc" in content, "module.yml missing pyfunc element"

    def test_frame_probe_exists(self):
        assert FRAME_PROBE.is_file(), f"Missing: {FRAME_PROBE}"
        content = FRAME_PROBE.read_text()
        assert "class MinimalFrameProbe" in content, "Frame probe class not found"
        assert "process_frame(self, buffer, frame_meta)" in content, "process_frame method not found"
        assert "NvDsPyFuncPlugin" in content, "Frame probe must inherit NvDsPyFuncPlugin"

    def test_cameras_yml_exists(self):
        assert CAMERAS_YAML.is_file(), f"Missing: {CAMERAS_YAML}"

    def test_compose_contains_required_services(self):
        import yaml

        with open(COMPOSE_FILE) as f:
            cfg = yaml.safe_load(f)
        services = cfg.get("services", {})
        assert SAVANT_SERVICE in services, f"Missing {SAVANT_SERVICE} service"
        assert RTSP_SERVICE in services, f"Missing {RTSP_SERVICE} service"
        assert FFMPEG_SERVICE in services, f"Missing {FFMPEG_SERVICE} service"
        assert REDIS_SERVICE in services, f"Missing {REDIS_SERVICE} service"

    def test_compose_rtsp_address_configured(self):
        import yaml

        with open(COMPOSE_FILE) as f:
            cfg = yaml.safe_load(f)
        env = cfg.get("services", {}).get(SAVANT_SERVICE, {}).get("environment", {})
        src_url = env.get("SRC_URL", "")
        assert "rtsp://" in src_url, f"RTSP address not found in SRC_URL: {src_url}"

    def test_compose_gpu_configured(self):
        import yaml

        with open(COMPOSE_FILE) as f:
            cfg = yaml.safe_load(f)
        deploy = cfg.get("services", {}).get(SAVANT_SERVICE, {}).get("deploy", {})
        devices = (
            deploy.get("resources", {})
            .get("reservations", {})
            .get("devices", [])
        )
        assert any("gpu" in str(d.get("capabilities", [])) for d in devices), (
            "GPU not configured for savant service"
        )


# ------------------------------------------------------------------
# Docker / GPU smoke test (requires --gpu flag)
# ------------------------------------------------------------------


@pytest.fixture(scope="module")
def dc_ensure_up():
    """Start compose, wait for health, yield, then tear down."""
    # Ensure host dirs exist
    for d in ["/data/video-analytics/models", "/data/video-analytics/downloads", "/data/video-analytics/media"]:
        Path(d).mkdir(parents=True, exist_ok=True)

    # Compose up
    result = _dc("up", "-d")
    assert result.returncode == 0, f"compose up failed:\n{result.stderr}"

    # Wait for Savant to initialize
    time.sleep(10)

    yield

    # Teardown
    _dc("down", "-v")


@pytest.mark.gpu
class TestPipelineGpuSmoke:
    """Full GPU smoke test — requires Docker + NVIDIA GPU."""

    def test_compose_up_success(self, dc_ensure_up):
        """Compose was already started by fixture."""
        result = _dc("ps", "--format", "json")
        assert result.returncode == 0, f"compose ps failed:\n{result.stderr}"
        assert "phase1c-savant" in result.stdout, "savant container not running"

    def test_savant_logs_show_frame_processing(self, dc_ensure_up):
        """Wait for Savant to process frames and check logs for frame probe output."""
        # Wait for pipeline initialization (might need longer for GPU model loading)
        time.sleep(15)
        result = _dc("logs", "--tail", "100", SAVANT_SERVICE)
        assert result.returncode == 0, f"logs failed:\n{result.stderr}"
        logs = result.stdout
        # Check that frame probe is logging
        assert "Frame #" in logs or "frame_probe" in logs, (
            f"Frame probe not detected in Savant logs.\nLogs tail:\n{logs[-2000:]}"
        )

    def test_savant_frame_count_increases(self, dc_ensure_up):
        """Check that Savant frame count increases over time (log-based)."""
        # Get initial frame count from logs
        result_before = _dc("logs", "--tail", "50", SAVANT_SERVICE)
        assert result_before.returncode == 0, f"logs failed:\n{result_before.stderr}"
        before_count = sum(
            1 for line in result_before.stdout.split("\n") if "Frame #" in line
        )

        # Wait for more frames
        time.sleep(10)

        # Get frame count again
        result_after = _dc("logs", "--tail", "100", SAVANT_SERVICE)
        assert result_after.returncode == 0, f"logs failed:\n{result_after.stderr}"
        after_count = sum(
            1 for line in result_after.stdout.split("\n") if "Frame #" in line
        )

        assert after_count > before_count, (
            f"Frame count did not increase (before={before_count} after={after_count}). "
            "Savant is not processing frames."
        )

    def test_ffmpeg_source_is_streaming(self, dc_ensure_up):
        """Check ffmpeg logs for successful streaming."""
        result = _dc("logs", "--tail", "20", FFMPEG_SERVICE)
        assert result.returncode == 0, f"ffmpeg logs failed:\n{result.stderr}"
        logs = result.stdout
        # ffmpeg should not be in error state
        assert "Error" not in logs, f"ffmpeg reported errors:\n{logs}"

    def test_container_stays_up(self, dc_ensure_up):
        """Verify containers remain running (not restart-looping)."""
        result = _dc("ps", "--format", "json")
        assert result.returncode == 0
        # Each container should show "Up" or "running"
        assert "running" in result.stdout.lower() or "Up" in result.stdout
