"""Tests for the savant_security camera config loader (camera config.1).

Loader reads a cameras.yml file (midterm camera config schema) and exposes lookup
helpers. Pure Python — no Savant / DB / HTTP / Savant. The tests use
``tmp_path`` to write fixture YAML files on disk.
"""

from __future__ import annotations

import importlib
import sys
import textwrap
from pathlib import Path

import pytest

MODULE_DIR = str(Path(__file__).resolve().parents[2] / "modules" / "savant_security")
MODULES_ROOT = str(Path(__file__).resolve().parents[2] / "modules")


def _isolate_savant_security_modules():
    """See test_rule_registry: drop sibling runtime paths and cached custom.*"""
    sys.path[:] = [
        p for p in sys.path
        if not (p.startswith(MODULES_ROOT) and p != MODULE_DIR)
    ]
    if MODULE_DIR not in sys.path:
        sys.path.insert(0, MODULE_DIR)
    for name in [m for m in list(sys.modules) if m == "custom" or m.startswith("custom.")]:
        sys.modules.pop(name, None)


@pytest.fixture()
def camera_config_module():
    _isolate_savant_security_modules()
    return importlib.import_module("custom.services.camera_config")


# ---------------------------------------------------------------------------
# Fixture YAML
# ---------------------------------------------------------------------------


VALID_YAML = textwrap.dedent("""
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
      cam_off:
        enabled: false
        source_id: primary_rtsp_off
        name: Disabled Cam
        rtsp_url: rtsp://example.local/off
        gpu_id: 0
        zones: {}
        rules: {}
""").strip()


@pytest.fixture()
def valid_yaml_file(tmp_path):
    p = tmp_path / "cameras.yml"
    p.write_text(VALID_YAML)
    return str(p)


# ===========================================================================
# 1. loader reads the midterm export YAML
# ===========================================================================


def test_load_camera_config_reads_yaml(camera_config_module, valid_yaml_file):
    bundle = camera_config_module.load_camera_config(valid_yaml_file)
    assert set(bundle.cameras.keys()) == {"cam_001", "cam_off"}


# ===========================================================================
# 2. lookup by camera_id
# ===========================================================================


def test_get_camera_by_id(camera_config_module, valid_yaml_file):
    bundle = camera_config_module.load_camera_config(valid_yaml_file)
    cam = bundle.get_camera("cam_001")
    assert cam is not None
    assert cam.source_id == "primary_rtsp"
    assert cam.rtsp_url == "rtsp://example.local/stream"
    assert cam.enabled is True
    assert cam.gpu_id == 0
    assert cam.location == "Test Area"


def test_get_camera_missing_returns_none(camera_config_module, valid_yaml_file):
    bundle = camera_config_module.load_camera_config(valid_yaml_file)
    assert bundle.get_camera("ghost") is None


# ===========================================================================
# 3. lookup by source_id
# ===========================================================================


def test_get_by_source_id(camera_config_module, valid_yaml_file):
    bundle = camera_config_module.load_camera_config(valid_yaml_file)
    cam = bundle.get_by_source_id("primary_rtsp")
    assert cam is not None
    assert cam.camera_id == "cam_001"


def test_get_by_source_id_missing(camera_config_module, valid_yaml_file):
    bundle = camera_config_module.load_camera_config(valid_yaml_file)
    assert bundle.get_by_source_id("nope") is None


# ===========================================================================
# 4. zone lookup
# ===========================================================================


def test_get_zone(camera_config_module, valid_yaml_file):
    bundle = camera_config_module.load_camera_config(valid_yaml_file)
    zone = bundle.get_zone("cam_001", "perimeter")
    assert zone is not None
    assert zone.type == "polygon"
    assert zone.points == [
        [100.0, 300.0],
        [900.0, 300.0],
        [900.0, 700.0],
        [100.0, 700.0],
    ]


def test_get_zone_missing(camera_config_module, valid_yaml_file):
    bundle = camera_config_module.load_camera_config(valid_yaml_file)
    assert bundle.get_zone("cam_001", "ghost") is None
    assert bundle.get_zone("ghost", "perimeter") is None


# ===========================================================================
# 5. intrusion rule lookup
# ===========================================================================


def test_get_intrusion_rule(camera_config_module, valid_yaml_file):
    bundle = camera_config_module.load_camera_config(valid_yaml_file)
    rule = bundle.get_rule("cam_001", "intrusion")
    assert rule is not None
    assert rule.rule_id == "intrusion"
    assert rule.algorithm_id == "behavior.intrusion"
    assert rule.rule_type == "intrusion"
    assert rule.enabled is True
    assert rule.config["zone"] == "perimeter"
    assert rule.config["min_inside_ms"] == 1000
    assert rule.config["cooldown_s"] == 30
    assert rule.config["severity"] == "medium"
    assert rule.config["snapshot_required"] is True
    assert rule.config["clip_required"] is True


def test_rule_config_strips_enabled_key(camera_config_module, valid_yaml_file):
    """``enabled`` lives on RuleEntry, not in config — avoids double bookkeeping."""
    bundle = camera_config_module.load_camera_config(valid_yaml_file)
    rule = bundle.get_rule("cam_001", "intrusion")
    assert "enabled" not in rule.config


def test_load_generated_algorithm_rule_shape(camera_config_module, tmp_path):
    yaml_text = textwrap.dedent("""
        cameras:
          cam_001:
            enabled: true
            source_id: primary_rtsp
            name: Cam
            input:
              type: rtsp
              rtsp_url: rtsp://x
              rtsp_transport: tcp
            zones:
              perimeter:
                zone_id: perimeter
                zone_type: polygon
                points: [[0,0],[10,0],[10,10],[0,10]]
            rules:
              intrusion_lobby:
                rule_id: intrusion_lobby
                algorithm_id: behavior.intrusion
                enabled: true
                config:
                  zone_id: perimeter
                  min_inside_ms: 1000
                  cooldown_s: 30
              watchlist_main:
                rule_id: watchlist_main
                algorithm_id: face.watchlist
                enabled: true
                config:
                  threshold: 0.75
    """).strip()
    p = tmp_path / "generated.yml"
    p.write_text(yaml_text)
    bundle = camera_config_module.load_camera_config(str(p))
    cam = bundle.get_camera("cam_001")
    assert cam.rtsp_url == "rtsp://x"
    assert set(cam.rules) == {"intrusion_lobby", "watchlist_main"}
    rule = cam.rules["intrusion_lobby"]
    assert rule.algorithm_id == "behavior.intrusion"
    assert rule.config["zone"] == "perimeter"


# ===========================================================================
# 6. iter_enabled_cameras excludes disabled
# ===========================================================================


def test_iter_enabled_cameras_excludes_disabled(camera_config_module, valid_yaml_file):
    bundle = camera_config_module.load_camera_config(valid_yaml_file)
    enabled_ids = [cam.camera_id for cam in bundle.iter_enabled_cameras()]
    assert enabled_ids == ["cam_001"]


def test_iter_enabled_returns_stable_order(camera_config_module, tmp_path):
    """iter_enabled_cameras returns cameras sorted by id so the runtime
    sees a deterministic ordering across reloads."""
    yaml_text = textwrap.dedent("""
        cameras:
          z_cam:
            enabled: true
            source_id: src_z
            name: Z
            rtsp_url: rtsp://z
            zones: {}
            rules: {}
          a_cam:
            enabled: true
            source_id: src_a
            name: A
            rtsp_url: rtsp://a
            zones: {}
            rules: {}
    """).strip()
    p = tmp_path / "stable.yml"
    p.write_text(yaml_text)
    bundle = camera_config_module.load_camera_config(str(p))
    ids = [c.camera_id for c in bundle.iter_enabled_cameras()]
    assert ids == ["a_cam", "z_cam"]


# ===========================================================================
# 7. intrusion rule referencing missing zone fails
# ===========================================================================


def test_intrusion_unknown_zone_raises(camera_config_module, tmp_path):
    yaml_text = textwrap.dedent("""
        cameras:
          cam_001:
            enabled: true
            source_id: primary_rtsp
            name: Cam
            rtsp_url: rtsp://x
            zones:
              perimeter:
                type: polygon
                points: [[0,0],[10,0],[10,10],[0,10]]
            rules:
              intrusion:
                enabled: true
                zone: not_there
                min_inside_ms: 1000
                cooldown_s: 30
    """).strip()
    p = tmp_path / "bad.yml"
    p.write_text(yaml_text)
    with pytest.raises(camera_config_module.CameraConfigError) as exc_info:
        camera_config_module.load_camera_config(str(p))
    assert "not_there" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Bonus validation guards
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "patch_yaml,expected_fragment",
    [
        # min_inside_ms must be > 0
        ("""
            cameras:
              c:
                source_id: s
                name: n
                rtsp_url: rtsp://x
                zones:
                  z:
                    type: polygon
                    points: [[0,0],[1,0],[1,1]]
                rules:
                  intrusion:
                    zone: z
                    min_inside_ms: 0
                    cooldown_s: 1
        """, "min_inside_ms"),
        # cooldown_s must be non-negative integer
        ("""
            cameras:
              c:
                source_id: s
                name: n
                rtsp_url: rtsp://x
                zones:
                  z:
                    type: polygon
                    points: [[0,0],[1,0],[1,1]]
                rules:
                  intrusion:
                    zone: z
                    min_inside_ms: 100
                    cooldown_s: -1
        """, "cooldown_s"),
        # severity must be in the allowed set
        ("""
            cameras:
              c:
                source_id: s
                name: n
                rtsp_url: rtsp://x
                zones:
                  z:
                    type: polygon
                    points: [[0,0],[1,0],[1,1]]
                rules:
                  intrusion:
                    zone: z
                    min_inside_ms: 100
                    cooldown_s: 1
                    severity: critical
        """, "severity"),
        # polygon needs >= 3 points
        ("""
            cameras:
              c:
                source_id: s
                name: n
                rtsp_url: rtsp://x
                zones:
                  z:
                    type: polygon
                    points: [[0,0],[1,0]]
                rules: {}
        """, "3 to 10"),
        # duplicate source_id across cameras
        ("""
            cameras:
              a:
                source_id: same
                name: a
                rtsp_url: rtsp://x
                zones: {}
                rules: {}
              b:
                source_id: same
                name: b
                rtsp_url: rtsp://y
                zones: {}
                rules: {}
        """, "duplicate source_id"),
        # missing required string fields
        ("""
            cameras:
              c:
                source_id: ""
                name: n
                rtsp_url: rtsp://x
                zones: {}
                rules: {}
        """, "source_id"),
    ],
)
def test_invalid_config_raises(camera_config_module, tmp_path, patch_yaml, expected_fragment):
    p = tmp_path / "bad.yml"
    p.write_text(textwrap.dedent(patch_yaml).strip())
    with pytest.raises(camera_config_module.CameraConfigError) as exc_info:
        camera_config_module.load_camera_config(str(p))
    assert expected_fragment in str(exc_info.value)


def test_missing_file_raises(camera_config_module, tmp_path):
    with pytest.raises(FileNotFoundError):
        camera_config_module.load_camera_config(str(tmp_path / "no_such_file.yml"))


def test_empty_yaml_yields_empty_bundle(camera_config_module, tmp_path):
    """A file with no cameras is valid (operator hasn't configured any yet)."""
    p = tmp_path / "empty.yml"
    p.write_text("cameras: {}\n")
    bundle = camera_config_module.load_camera_config(str(p))
    assert bundle.cameras == {}
    assert list(bundle.iter_enabled_cameras()) == []


def test_loader_does_not_import_savant_or_db():
    """Architectural guard: the loader module must not import Savant or db drivers."""
    text = (Path(MODULE_DIR) / "custom" / "services" / "camera_config.py").read_text()
    for forbidden in ("from savant", "import savant", "import psycopg",
                      "from psycopg", "import requests", "import httpx"):
        assert forbidden not in text, f"camera_config.py must not contain: {forbidden}"
