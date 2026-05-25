"""Tests for the combined runtime config exporter (Phase C1.2)."""

from __future__ import annotations

import importlib.util
import textwrap
from pathlib import Path
from typing import List

import pytest
import yaml


SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "config" / "export_runtime_configs.py"


def _load_script_module():
    spec = importlib.util.spec_from_file_location(
        "export_runtime_configs_under_test", SCRIPT_PATH
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def script_mod():
    return _load_script_module()


SAMPLE_API_RESPONSE = textwrap.dedent("""
    cameras:
      cam_001:
        enabled: true
        source_id: phase3h
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
      cam_disabled:
        enabled: false
        source_id: off_source
        name: Disabled
        rtsp_url: rtsp://example.local/off
        gpu_id: 0
""").strip() + "\n"


class _FakeFetcher:
    def __init__(self, body: str = SAMPLE_API_RESPONSE):
        self.body = body
        self.urls: List[str] = []

    def __call__(self, url: str) -> str:
        self.urls.append(url)
        return self.body


# ===========================================================================
# 1. generates module-side cameras.generated.yml
# ===========================================================================


def test_module_config_is_written_verbatim(script_mod, tmp_path):
    fetcher = _FakeFetcher()
    module_out = tmp_path / "cameras.generated.yml"
    sources_out = tmp_path / "sources.generated.yml"

    rc = script_mod.main(
        [
            "--api-base-url", "http://api:8001",
            "--module-config-output", str(module_out),
            "--sources-output", str(sources_out),
            # include_disabled=true so cam_disabled is in the input doc
            "--include-disabled",
        ],
        fetcher=fetcher,
        logger=lambda msg: None,
    )
    assert rc == 0
    assert module_out.read_text() == SAMPLE_API_RESPONSE
    assert fetcher.urls == [
        "http://api:8001/api/v1/cameras/config/export?include_disabled=true"
    ]


# ===========================================================================
# 2. generates adapter-side sources.generated.yml
# ===========================================================================


def test_sources_yaml_has_expected_shape(script_mod, tmp_path):
    fetcher = _FakeFetcher()
    module_out = tmp_path / "cameras.generated.yml"
    sources_out = tmp_path / "sources.generated.yml"
    rc = script_mod.main(
        [
            "--api-base-url", "http://api:8001",
            "--module-config-output", str(module_out),
            "--sources-output", str(sources_out),
            "--include-disabled",
        ],
        fetcher=fetcher,
        logger=lambda msg: None,
    )
    assert rc == 0
    doc = yaml.safe_load(sources_out.read_text())
    assert "sources" in doc
    assert set(doc["sources"]) == {"cam_001", "cam_disabled"}

    entry = doc["sources"]["cam_001"]
    assert entry["camera_id"] == "cam_001"
    assert entry["source_id"] == "phase3h"
    assert entry["uri"] == "rtsp://example.local/stream"
    assert entry["enabled"] is True
    assert entry["adapter_type"] == "gstreamer"
    assert entry["zmq_endpoint"].startswith("dealer+connect:")


# ===========================================================================
# 3. disabled camera enabled flag is preserved
# ===========================================================================


def test_disabled_source_kept_with_enabled_false(script_mod, tmp_path):
    fetcher = _FakeFetcher()
    module_out = tmp_path / "cameras.generated.yml"
    sources_out = tmp_path / "sources.generated.yml"
    rc = script_mod.main(
        [
            "--api-base-url", "http://api:8001",
            "--module-config-output", str(module_out),
            "--sources-output", str(sources_out),
            "--include-disabled",
        ],
        fetcher=fetcher,
        logger=lambda msg: None,
    )
    assert rc == 0
    doc = yaml.safe_load(sources_out.read_text())
    assert doc["sources"]["cam_disabled"]["enabled"] is False
    assert doc["sources"]["cam_001"]["enabled"] is True


# ===========================================================================
# 4. source_id / camera_id / uri mapping is correct
# ===========================================================================


def test_mapping_is_correct(script_mod, tmp_path):
    fetcher = _FakeFetcher()
    module_out = tmp_path / "cameras.generated.yml"
    sources_out = tmp_path / "sources.generated.yml"
    script_mod.main(
        [
            "--api-base-url", "http://api:8001",
            "--module-config-output", str(module_out),
            "--sources-output", str(sources_out),
        ],
        fetcher=fetcher,
        logger=lambda msg: None,
    )
    sources_doc = yaml.safe_load(sources_out.read_text())
    cameras_doc = yaml.safe_load(module_out.read_text())
    for cam_id, src in sources_doc["sources"].items():
        assert src["camera_id"] == cam_id
        assert src["source_id"] == cameras_doc["cameras"][cam_id]["source_id"]
        assert src["uri"] == cameras_doc["cameras"][cam_id]["rtsp_url"]


# ===========================================================================
# 5. log never contains rtsp_url
# ===========================================================================


def test_log_does_not_include_rtsp_url(script_mod, tmp_path):
    fetcher = _FakeFetcher()
    logs: List[str] = []
    module_out = tmp_path / "cameras.generated.yml"
    sources_out = tmp_path / "sources.generated.yml"
    script_mod.main(
        [
            "--api-base-url", "http://api:8001",
            "--module-config-output", str(module_out),
            "--sources-output", str(sources_out),
        ],
        fetcher=fetcher,
        logger=logs.append,
    )
    joined = "\n".join(logs)
    # The URI scheme + body must never appear in the log. We don't
    # match the bare substring "rtsp" because pytest's tmp_path
    # directory names include the test name (which contains "rtsp").
    assert "rtsp://" not in joined
    assert "example.local/stream" not in joined
    # But the safe fields ARE logged.
    assert "cam_001" in joined
    assert "phase3h" in joined


# ===========================================================================
# 6. multiple enabled cameras coexist (controller default still requires
#    explicit --source-id at start time — covered in the controller tests)
# ===========================================================================


def test_multiple_enabled_cameras_in_sources(script_mod, tmp_path):
    multi_body = textwrap.dedent("""
        cameras:
          cam_a:
            enabled: true
            source_id: src_a
            name: A
            rtsp_url: rtsp://a/stream
            gpu_id: 0
          cam_b:
            enabled: true
            source_id: src_b
            name: B
            rtsp_url: rtsp://b/stream
            gpu_id: 0
    """).strip() + "\n"
    fetcher = _FakeFetcher(body=multi_body)
    module_out = tmp_path / "cameras.generated.yml"
    sources_out = tmp_path / "sources.generated.yml"
    rc = script_mod.main(
        [
            "--api-base-url", "http://api:8001",
            "--module-config-output", str(module_out),
            "--sources-output", str(sources_out),
        ],
        fetcher=fetcher,
        logger=lambda msg: None,
    )
    assert rc == 0
    doc = yaml.safe_load(sources_out.read_text())
    assert set(doc["sources"]) == {"cam_a", "cam_b"}
    assert doc["sources"]["cam_a"]["source_id"] == "src_a"
    assert doc["sources"]["cam_b"]["source_id"] == "src_b"


# ---------------------------------------------------------------------------
# Bonus failure-path guards
# ---------------------------------------------------------------------------


def test_nonzero_on_transport_error(script_mod, tmp_path):
    def broken(url: str) -> str:
        raise RuntimeError("network down")

    module_out = tmp_path / "cameras.generated.yml"
    sources_out = tmp_path / "sources.generated.yml"
    rc = script_mod.main(
        [
            "--api-base-url", "http://api:8001",
            "--module-config-output", str(module_out),
            "--sources-output", str(sources_out),
        ],
        fetcher=broken,
        logger=lambda msg: None,
    )
    assert rc != 0
    assert not module_out.exists()
    assert not sources_out.exists()


def test_parent_dirs_created_for_both_outputs(script_mod, tmp_path):
    fetcher = _FakeFetcher()
    module_out = tmp_path / "deep" / "nested1" / "cameras.generated.yml"
    sources_out = tmp_path / "deep" / "nested2" / "sources.generated.yml"
    rc = script_mod.main(
        [
            "--api-base-url", "http://api:8001",
            "--module-config-output", str(module_out),
            "--sources-output", str(sources_out),
        ],
        fetcher=fetcher,
        logger=lambda msg: None,
    )
    assert rc == 0
    assert module_out.exists() and sources_out.exists()
