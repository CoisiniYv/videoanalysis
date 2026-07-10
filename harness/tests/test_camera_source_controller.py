"""Tests for the camera source controller (midterm).

The controller's only side-effect is invoking ``docker`` via subprocess.
Tests inject a fake runner so no Docker daemon, network, or container
is required.
"""

from __future__ import annotations

import importlib.util
import sys
import textwrap
from pathlib import Path
from typing import Any, List

import pytest
import subprocess


SCRIPT_PATH = (
    Path(__file__).resolve().parents[2]
    / "scripts" / "runtime" / "camera_source_controller.py"
)


def _load_script_module():
    spec = importlib.util.spec_from_file_location(
        "camera_source_controller_under_test", SCRIPT_PATH
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    # Register in sys.modules so @dataclass can introspect the module
    # back via cls.__module__ → sys.modules.get(name).__dict__.
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def script_mod():
    return _load_script_module()


SAMPLE_SOURCES = textwrap.dedent("""
    sources:
      cam_001:
        camera_id: cam_001
        source_id: primary_rtsp
        uri: rtsp://example.local/stream
        enabled: true
        adapter_type: gstreamer
        zmq_endpoint: dealer+connect:tcp://savant-security:5555
      cam_file:
        camera_id: cam_file
        source_id: file_loop
        uri: file:///testVideo/test.mp4
        enabled: true
        adapter_type: gstreamer
        zmq_endpoint: dealer+connect:tcp://savant-security:5555
      cam_disabled:
        camera_id: cam_disabled
        source_id: disabled_source
        uri: rtsp://example.local/off
        enabled: false
        adapter_type: gstreamer
        zmq_endpoint: dealer+connect:tcp://savant-security:5555
""").strip() + "\n"


@pytest.fixture()
def sources_path(tmp_path):
    p = tmp_path / "sources.generated.yml"
    p.write_text(SAMPLE_SOURCES)
    return str(p)


class _FakeRunner:
    """Captures invocations; returns whatever ``stub_returns.pop(0)`` provides."""

    def __init__(self, *returns: subprocess.CompletedProcess):
        self.calls: List[List[str]] = []
        self.returns = list(returns)

    def __call__(self, cmd: List[str]) -> subprocess.CompletedProcess:
        self.calls.append(list(cmd))
        if not self.returns:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        return self.returns.pop(0)


def _ok(stdout="", stderr=""):
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout, stderr=stderr)


def _fail(stderr="failure", returncode=1):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout="", stderr=stderr)


# ===========================================================================
# 1. list output never contains the RTSP URL
# ===========================================================================


def test_list_redacts_uri(script_mod, sources_path):
    logs: List[str] = []
    rc = script_mod.main(
        ["list", "--sources", sources_path],
        runner=lambda cmd: _ok(),
        logger=logs.append,
    )
    assert rc == 0
    joined = "\n".join(logs)
    assert "rtsp://" not in joined
    assert "example.local/stream" not in joined
    # Safe fields should be present.
    assert "primary_rtsp" in joined
    assert "scheme" in joined


# ===========================================================================
# 2. start builds correct docker command with SOURCE_ID / LOCATION / ZMQ
# ===========================================================================


def test_start_builds_docker_run(script_mod, sources_path):
    runner = _FakeRunner(_ok(stdout="abc123def456"))
    rc = script_mod.main(
        [
            "start",
            "--sources", sources_path,
            "--source-id", "primary_rtsp",
            "--network", "video-analytics-midterm_default",
        ],
        runner=runner,
        logger=lambda msg: None,
    )
    assert rc == 0
    assert len(runner.calls) == 1
    cmd = runner.calls[0]
    assert cmd[:4] == ["docker", "run", "-d", "--name"]
    assert "video-analytics-source-primary_rtsp" in cmd
    # Env vars are passed via -e KEY=VALUE; verify SOURCE_ID + ZMQ_ENDPOINT.
    env_pairs = [cmd[i + 1] for i, p in enumerate(cmd) if p == "-e"]
    env_dict = dict(s.split("=", 1) for s in env_pairs)
    assert env_dict["SOURCE_ID"] == "primary_rtsp"
    assert env_dict["LOCATION"] == "rtsp://example.local/stream"
    assert env_dict["RTSP_URI"] == "rtsp://example.local/stream"
    assert env_dict["RTSP_TRANSPORT"] == script_mod.DEFAULT_RTSP_TRANSPORT_PARAMS
    assert env_dict["ZMQ_ENDPOINT"] == "dealer+connect:tcp://savant-security:5555"
    assert env_dict["SYNC_OUTPUT"] == "false"
    assert env_dict["BUFFER_LEN"] == "2000"
    assert env_dict["EOS_ON_START"] == "false"
    assert "USE_ABSOLUTE_TIMESTAMPS" not in env_dict
    assert env_dict["FFMPEG_TIMEOUT_MS"] == "20000"
    # rtsp scheme -> rtsp.sh entrypoint.
    assert "/opt/savant/adapters/gst/sources/rtsp.sh" in cmd


def test_start_accepts_custom_ffmpeg_timeout(script_mod, sources_path):
    runner = _FakeRunner(_ok(stdout="abc123def456"))
    rc = script_mod.main(
        [
            "start",
            "--sources", sources_path,
            "--source-id", "primary_rtsp",
            "--ffmpeg-timeout-ms", "60000",
        ],
        runner=runner,
        logger=lambda msg: None,
    )
    assert rc == 0
    env_pairs = [
        runner.calls[0][i + 1]
        for i, p in enumerate(runner.calls[0])
        if p == "-e"
    ]
    env_dict = dict(s.split("=", 1) for s in env_pairs)
    assert env_dict["FFMPEG_TIMEOUT_MS"] == "60000"


# ===========================================================================
# 3. stop uses the stable container name
# ===========================================================================


def test_stop_uses_stable_container_name(script_mod):
    runner = _FakeRunner(_ok())
    rc = script_mod.main(
        ["stop", "--source-id", "primary_rtsp"],
        runner=runner,
        logger=lambda msg: None,
    )
    assert rc == 0
    assert runner.calls == [
        ["docker", "rm", "-f", "video-analytics-source-primary_rtsp"]
    ]


# ===========================================================================
# 4. status queries the stable container name
# ===========================================================================


def test_status_uses_stable_container_name(script_mod):
    runner = _FakeRunner(_ok(stdout="video-analytics-source-primary_rtsp\tUp 3 minutes"))
    logs: List[str] = []
    rc = script_mod.main(
        ["status", "--source-id", "primary_rtsp"],
        runner=runner,
        logger=logs.append,
    )
    assert rc == 0
    cmd = runner.calls[0]
    assert cmd[:3] == ["docker", "ps", "-a"]
    assert "--filter" in cmd
    filter_idx = cmd.index("--filter")
    assert cmd[filter_idx + 1] == "name=^/video-analytics-source-primary_rtsp$"
    assert any("Up 3 minutes" in line for line in logs)


# ===========================================================================
# 5. unknown source_id returns non-zero
# ===========================================================================


def test_start_unknown_source_id(script_mod, sources_path):
    runner = _FakeRunner()
    logs: List[str] = []
    rc = script_mod.main(
        ["start", "--sources", sources_path, "--source-id", "does_not_exist"],
        runner=runner,
        logger=logs.append,
    )
    assert rc != 0
    assert runner.calls == []
    assert any("not found" in line for line in logs)


# ===========================================================================
# 6. disabled source cannot be started
# ===========================================================================


def test_start_disabled_source_refused(script_mod, sources_path):
    runner = _FakeRunner()
    logs: List[str] = []
    rc = script_mod.main(
        ["start", "--sources", sources_path, "--source-id", "disabled_source"],
        runner=runner,
        logger=logs.append,
    )
    assert rc != 0
    assert runner.calls == []
    assert any("disabled" in line for line in logs)


# ===========================================================================
# 7. docker failure surfaces non-zero exit code
# ===========================================================================


def test_start_docker_failure_returns_nonzero(script_mod, sources_path):
    runner = _FakeRunner(_fail(stderr="image pull failed", returncode=125))
    logs: List[str] = []
    rc = script_mod.main(
        ["start", "--sources", sources_path, "--source-id", "primary_rtsp"],
        runner=runner,
        logger=logs.append,
    )
    assert rc != 0
    # rm -f wasn't attempted because start failed before that.
    assert len(runner.calls) == 1


def test_stop_docker_failure_returns_nonzero(script_mod):
    runner = _FakeRunner(_fail(stderr="no such container"))
    rc = script_mod.main(
        ["stop", "--source-id", "no_such"],
        runner=runner,
        logger=lambda msg: None,
    )
    assert rc != 0


# ===========================================================================
# 8. file:// scheme switches entrypoint
# ===========================================================================


def test_file_uri_uses_video_loop_entrypoint(script_mod, sources_path):
    runner = _FakeRunner(_ok())
    rc = script_mod.main(
        ["start", "--sources", sources_path, "--source-id", "file_loop"],
        runner=runner,
        logger=lambda msg: None,
    )
    assert rc == 0
    cmd = runner.calls[0]
    assert "/opt/savant/adapters/gst/sources/video_loop.sh" in cmd
    env_pairs = [cmd[i + 1] for i, p in enumerate(cmd) if p == "-e"]
    env_dict = dict(s.split("=", 1) for s in env_pairs)
    # file:// prefix stripped so LOCATION is a plain path the script can open.
    assert env_dict["LOCATION"] == "/testVideo/test.mp4"
    assert env_dict["EOS_ON_START"] == "false"
    assert "USE_ABSOLUTE_TIMESTAMPS" not in env_dict


# ---------------------------------------------------------------------------
# Bonus: log does not leak the URI even when start succeeds
# ---------------------------------------------------------------------------


def test_start_log_never_contains_rtsp_uri(script_mod, sources_path):
    runner = _FakeRunner(_ok(stdout="deadbeef"))
    logs: List[str] = []
    rc = script_mod.main(
        ["start", "--sources", sources_path, "--source-id", "primary_rtsp"],
        runner=runner,
        logger=logs.append,
    )
    assert rc == 0
    joined = "\n".join(logs)
    assert "example.local/stream" not in joined
    assert "rtsp://" not in joined
    assert "rtsp-uri-redacted" in joined or "uri-redacted" in joined


def test_duplicate_source_id_in_sources_file_fails_load(script_mod, tmp_path):
    yaml_text = textwrap.dedent("""
        sources:
          cam_a:
            camera_id: cam_a
            source_id: same_src
            uri: rtsp://a
            enabled: true
            adapter_type: gstreamer
            zmq_endpoint: dealer+connect:tcp://savant:5555
          cam_b:
            camera_id: cam_b
            source_id: same_src
            uri: rtsp://b
            enabled: true
            adapter_type: gstreamer
            zmq_endpoint: dealer+connect:tcp://savant:5555
    """).strip() + "\n"
    p = tmp_path / "sources.generated.yml"
    p.write_text(yaml_text)
    logs: List[str] = []
    rc = script_mod.main(
        ["list", "--sources", str(p)],
        runner=lambda cmd: _ok(),
        logger=logs.append,
    )
    assert rc != 0
    assert any("duplicate source_id" in line for line in logs)


def test_unknown_sources_file_returns_nonzero(script_mod, tmp_path):
    logs: List[str] = []
    rc = script_mod.main(
        ["list", "--sources", str(tmp_path / "missing.yml")],
        runner=lambda cmd: _ok(),
        logger=logs.append,
    )
    assert rc != 0
