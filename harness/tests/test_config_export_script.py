"""Tests for the midterm export script (scripts/config/export_cameras_yml.py).

These tests inject a fake HTTP fetcher into ``main(...)`` so no live
API is needed. They verify that:
- raw YAML responses are written verbatim,
- the output is parseable by yaml.safe_load,
- the parent directory is created on demand,
- the include_disabled flag is propagated as a query param,
- network or YAML failures yield a non-zero exit code,
- the console log never includes rtsp_url.
"""

from __future__ import annotations

import importlib.util
import sys
import textwrap
from pathlib import Path
from typing import List

import pytest
import yaml


SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "config" / "export_cameras_yml.py"


def _load_script_module():
    """Import the script by file path so tests don't need it on PYTHONPATH."""
    spec = importlib.util.spec_from_file_location(
        "export_cameras_yml_under_test", SCRIPT_PATH
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def script_mod():
    return _load_script_module()


# ---------------------------------------------------------------------------
# Fixture YAML body (matches what GET /api/v1/cameras/config/export returns)
# ---------------------------------------------------------------------------


SAMPLE_YAML = textwrap.dedent("""
    cameras:
      cam_001:
        enabled: true
        source_id: primary_rtsp
        name: Test Camera
        rtsp_url: rtsp://example.local/stream
        gpu_id: 0
        location: Test Area
        zones:
          perimeter:
            type: polygon
            points:
              - [100, 300]
              - [900, 300]
              - [900, 700]
              - [100, 700]
        rules:
          intrusion:
            enabled: true
            zone: perimeter
            min_inside_ms: 1000
            cooldown_s: 30
            severity: medium
            snapshot_required: true
            clip_required: true
""").strip() + "\n"


class _FakeFetcher:
    def __init__(self, body: str = SAMPLE_YAML):
        self.body = body
        self.urls: List[str] = []

    def __call__(self, url: str) -> str:
        self.urls.append(url)
        return self.body


# ===========================================================================
# 8. export script processes a raw YAML response
# ===========================================================================


def test_main_writes_file_with_response_body(script_mod, tmp_path):
    fetcher = _FakeFetcher()
    output = tmp_path / "subdir" / "cameras.midterm.yml"
    rc = script_mod.main(
        [
            "--api-base-url", "http://api:8001",
            "--output", str(output),
        ],
        fetcher=fetcher,
        logger=lambda msg: None,
    )
    assert rc == 0
    assert output.exists()
    assert output.read_text() == SAMPLE_YAML
    assert fetcher.urls == ["http://api:8001/api/v1/cameras/config/export"]


# ===========================================================================
# 9. output is parseable by yaml.safe_load
# ===========================================================================


def test_main_output_is_parseable_yaml(script_mod, tmp_path):
    fetcher = _FakeFetcher()
    output = tmp_path / "cameras.midterm.yml"
    rc = script_mod.main(
        ["--api-base-url", "http://api:8001", "--output", str(output)],
        fetcher=fetcher,
        logger=lambda msg: None,
    )
    assert rc == 0
    doc = yaml.safe_load(output.read_text())
    assert "cameras" in doc
    assert doc["cameras"]["cam_001"]["source_id"] == "primary_rtsp"
    assert doc["cameras"]["cam_001"]["zones"]["perimeter"]["type"] == "polygon"


# ===========================================================================
# 10. non-zero exit code on API failure
# ===========================================================================


def test_main_returns_nonzero_on_transport_error(script_mod, tmp_path):
    def broken_fetch(url: str) -> str:
        raise RuntimeError("connection refused")

    output = tmp_path / "cameras.midterm.yml"
    logs: List[str] = []
    rc = script_mod.main(
        ["--api-base-url", "http://api:8001", "--output", str(output)],
        fetcher=broken_fetch,
        logger=logs.append,
    )
    assert rc != 0
    assert not output.exists()
    assert any("fetch failed" in line for line in logs)


def test_main_returns_nonzero_on_invalid_yaml(script_mod, tmp_path):
    def garbage_fetch(url: str) -> str:
        return "this: is:\n  - not\nvalid: ::yaml"

    output = tmp_path / "cameras.midterm.yml"
    logs: List[str] = []
    rc = script_mod.main(
        ["--api-base-url", "http://api:8001", "--output", str(output)],
        fetcher=garbage_fetch,
        logger=logs.append,
    )
    assert rc != 0
    assert not output.exists()


def test_main_returns_nonzero_when_cameras_key_missing(script_mod, tmp_path):
    def missing_key_fetch(url: str) -> str:
        return "not_cameras: {}\n"

    output = tmp_path / "cameras.midterm.yml"
    logs: List[str] = []
    rc = script_mod.main(
        ["--api-base-url", "http://api:8001", "--output", str(output)],
        fetcher=missing_key_fetch,
        logger=logs.append,
    )
    assert rc != 0
    assert not output.exists()
    assert any("cameras" in line for line in logs)


# ---------------------------------------------------------------------------
# Bonus guards
# ---------------------------------------------------------------------------


def test_main_creates_parent_directory(script_mod, tmp_path):
    fetcher = _FakeFetcher()
    output = tmp_path / "deeply" / "nested" / "cameras.midterm.yml"
    assert not output.parent.exists()
    rc = script_mod.main(
        ["--api-base-url", "http://api:8001", "--output", str(output)],
        fetcher=fetcher,
        logger=lambda msg: None,
    )
    assert rc == 0
    assert output.exists()
    assert output.parent.is_dir()


def test_main_include_disabled_flag_appends_query(script_mod, tmp_path):
    fetcher = _FakeFetcher()
    output = tmp_path / "cameras.midterm.yml"
    rc = script_mod.main(
        [
            "--api-base-url", "http://api:8001",
            "--output", str(output),
            "--include-disabled",
        ],
        fetcher=fetcher,
        logger=lambda msg: None,
    )
    assert rc == 0
    assert fetcher.urls == [
        "http://api:8001/api/v1/cameras/config/export?include_disabled=true"
    ]


def test_main_log_does_not_include_rtsp_url(script_mod, tmp_path):
    """Operators copy logs into tickets — rtsp_url often carries credentials."""
    fetcher = _FakeFetcher()
    output = tmp_path / "cameras.midterm.yml"
    logs: List[str] = []
    rc = script_mod.main(
        ["--api-base-url", "http://api:8001", "--output", str(output)],
        fetcher=fetcher,
        logger=logs.append,
    )
    assert rc == 0
    joined = "\n".join(logs)
    assert "rtsp://example.local/stream" not in joined
    # But the safe fields ARE logged.
    assert "cam_001" in joined
    assert "primary_rtsp" in joined


def test_main_strips_trailing_slash_in_base_url(script_mod, tmp_path):
    fetcher = _FakeFetcher()
    output = tmp_path / "cameras.midterm.yml"
    rc = script_mod.main(
        ["--api-base-url", "http://api:8001/", "--output", str(output)],
        fetcher=fetcher,
        logger=lambda msg: None,
    )
    assert rc == 0
    assert fetcher.urls == ["http://api:8001/api/v1/cameras/config/export"]
