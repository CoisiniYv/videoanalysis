"""Polygon ROI validation tests (Phase C1.3).

Covers BOTH the API ZoneCreate validator (Pydantic) and the savant_security
camera_config loader. The two layers must agree on the 3..10 polygon bound;
any drift between them is a real bug, so the same parametrised matrix is run
against both implementations.
"""

from __future__ import annotations

import importlib
import sys
import textwrap
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
API_DIR = str(REPO_ROOT / "services" / "api")
MODULE_DIR = str(REPO_ROOT / "modules" / "savant_security")
MODULES_ROOT = str(REPO_ROOT / "modules")


# ---------------------------------------------------------------------------
# API schema fixture
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def api_schema():
    # Drop sibling app.* caches in case a phase test loaded a different copy.
    for name in [m for m in list(sys.modules) if m == "app" or m.startswith("app.")]:
        sys.modules.pop(name, None)
    if API_DIR not in sys.path:
        sys.path.insert(0, API_DIR)
    return importlib.import_module("app.schemas.cameras")


# ---------------------------------------------------------------------------
# Loader fixture
# ---------------------------------------------------------------------------


def _isolate_savant_security_modules():
    sys.path[:] = [
        p for p in sys.path
        if not (p.startswith(MODULES_ROOT) and p != MODULE_DIR)
    ]
    if MODULE_DIR not in sys.path:
        sys.path.insert(0, MODULE_DIR)
    for name in [m for m in list(sys.modules) if m == "custom" or m.startswith("custom.")]:
        sys.modules.pop(name, None)


@pytest.fixture()
def loader_mod():
    _isolate_savant_security_modules()
    return importlib.import_module("custom.services.camera_config")


def _write_yaml(tmp_path, text):
    p = tmp_path / "cameras.generated.yml"
    p.write_text(textwrap.dedent(text).strip() + "\n")
    return str(p)


# ---------------------------------------------------------------------------
# Helpers to build evenly-spaced polygons of arbitrary point count
# ---------------------------------------------------------------------------


def _square():
    return [[0, 0], [10, 0], [10, 10], [0, 10]]


def _pentagon():
    return [[0, 0], [10, 0], [12, 5], [5, 12], [-2, 5]]


def _polygon(n: int):
    """Return an n-point polygon. n in [2..15] covers our boundary cases."""
    pts = []
    for i in range(n):
        # spread points roughly on a circle
        pts.append([10 + i, 10 + i * 2])
    return pts


# ===========================================================================
# API ZoneCreate.validate_for_zone_type
# ===========================================================================


@pytest.mark.parametrize("n", [3, 4, 5, 7, 10])
def test_api_polygon_3_to_10_passes(api_schema, n):
    z = api_schema.ZoneCreate(
        zone_name="perimeter",
        zone_type="polygon",
        points=_polygon(n),
    )
    z.validate_for_zone_type()  # must not raise


@pytest.mark.parametrize("n,expected_fragment", [
    (2, "3 to 10"),
    (11, "3 to 10"),
    (15, "3 to 10"),
])
def test_api_polygon_out_of_bounds_fails(api_schema, n, expected_fragment):
    z = api_schema.ZoneCreate(
        zone_name="perimeter",
        zone_type="polygon",
        points=_polygon(n),
    )
    with pytest.raises(ValueError) as exc:
        z.validate_for_zone_type()
    assert expected_fragment in str(exc.value)


def test_api_polygon_point_must_be_xy_pair(api_schema):
    with pytest.raises(Exception):  # pydantic ValidationError
        api_schema.ZoneCreate(
            zone_name="perimeter",
            zone_type="polygon",
            points=[[0, 0], [10], [10, 10]],  # second point only has 1 coord
        )


def test_api_polygon_coords_must_be_numeric(api_schema):
    with pytest.raises(Exception):
        api_schema.ZoneCreate(
            zone_name="perimeter",
            zone_type="polygon",
            points=[[0, 0], ["a", "b"], [10, 10]],
        )


# Line / direction_line
def test_api_line_two_points_passes(api_schema):
    z = api_schema.ZoneCreate(
        zone_name="trip_wire",
        zone_type="line",
        points=[[0, 0], [10, 10]],
    )
    z.validate_for_zone_type()


def test_api_direction_line_two_points_passes(api_schema):
    z = api_schema.ZoneCreate(
        zone_name="trip_wire",
        zone_type="direction_line",
        points=[[0, 0], [10, 10]],
    )
    z.validate_for_zone_type()


def test_api_line_three_points_fails(api_schema):
    z = api_schema.ZoneCreate(
        zone_name="trip_wire",
        zone_type="line",
        points=[[0, 0], [5, 5], [10, 10]],
    )
    with pytest.raises(ValueError) as exc:
        z.validate_for_zone_type()
    assert "exactly 2" in str(exc.value)


def test_api_constants_exist(api_schema):
    """Lock the constants so future drift is visible."""
    assert api_schema.POLYGON_MIN_POINTS == 3
    assert api_schema.POLYGON_MAX_POINTS == 10


# ===========================================================================
# Loader (camera_config.load_camera_config)
# ===========================================================================


def _yaml_with_polygon(points_yaml: str) -> str:
    return f"""
        cameras:
          cam_001:
            enabled: true
            source_id: phase3h
            name: T
            rtsp_url: rtsp://x
            zones:
              perimeter:
                type: polygon
                points: {points_yaml}
            rules: {{}}
    """


@pytest.mark.parametrize("n", [3, 5, 10])
def test_loader_polygon_3_to_10_passes(loader_mod, tmp_path, n):
    pts = _polygon(n)
    cfg = _write_yaml(tmp_path, _yaml_with_polygon(str(pts)))
    bundle = loader_mod.load_camera_config(cfg)
    assert len(bundle.get_camera("cam_001").zones["perimeter"].points) == n


@pytest.mark.parametrize("n", [2, 11, 12])
def test_loader_polygon_out_of_bounds_raises(loader_mod, tmp_path, n):
    pts = _polygon(n)
    cfg = _write_yaml(tmp_path, _yaml_with_polygon(str(pts)))
    with pytest.raises(loader_mod.CameraConfigError) as exc:
        loader_mod.load_camera_config(cfg)
    msg = str(exc.value)
    # The error path must include camera / zone / points so operators
    # can find the offending field without grepping.
    assert "cameras.cam_001.zones.perimeter.points" in msg
    assert "3 to 10" in msg


def test_loader_line_must_have_two_points(loader_mod, tmp_path):
    cfg = _write_yaml(tmp_path, """
        cameras:
          cam_001:
            enabled: true
            source_id: phase3h
            name: T
            rtsp_url: rtsp://x
            zones:
              trip_wire:
                type: line
                points: [[0,0],[5,5],[10,10]]
            rules: {}
    """)
    with pytest.raises(loader_mod.CameraConfigError) as exc:
        loader_mod.load_camera_config(cfg)
    assert "exactly 2 points" in str(exc.value)
    assert "cameras.cam_001.zones.trip_wire.points" in str(exc.value)


def test_loader_constants_match_api(loader_mod):
    assert loader_mod.POLYGON_MIN_POINTS == 3
    assert loader_mod.POLYGON_MAX_POINTS == 10
