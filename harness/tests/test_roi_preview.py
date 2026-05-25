"""Tests for the preview-roi subcommand of camera_config_cli.py (Phase C1.3)."""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from typing import List

import pytest

PIL = pytest.importorskip("PIL")
from PIL import Image  # noqa: E402


SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "camera_config_cli.py"


def _load_cli():
    spec = importlib.util.spec_from_file_location(
        "camera_config_cli_preview_under_test", SCRIPT_PATH
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def cli():
    return _load_cli()


@pytest.fixture()
def sample_image(tmp_path):
    """Plain 800x600 grey JPEG to draw on."""
    img = Image.new("RGB", (800, 600), color=(64, 64, 64))
    path = tmp_path / "snapshot.jpg"
    img.save(path, quality=80)
    return str(path)


# ===========================================================================
# 1. preview generates a file from a 6-vertex polygon
# ===========================================================================


def test_preview_writes_output(cli, sample_image, tmp_path):
    out = tmp_path / "roi_preview.jpg"
    rc = cli.main(
        [
            "preview-roi",
            "--image", sample_image,
            "--polygon", "100,120;600,80;700,400;500,560;200,540;80,300",
            "--zone-name", "perimeter",
            "--output", str(out),
        ],
        logger=lambda msg: None,
    )
    assert rc == 0
    assert out.exists()
    assert out.stat().st_size > 1000  # non-trivial JPEG


# ===========================================================================
# 2. original image is not overwritten
# ===========================================================================


def test_preview_does_not_modify_source(cli, sample_image, tmp_path):
    original_bytes = Path(sample_image).read_bytes()
    out = tmp_path / "roi_preview.jpg"
    rc = cli.main(
        [
            "preview-roi",
            "--image", sample_image,
            "--polygon", "100,100;500,100;500,400;100,400",
            "--output", str(out),
        ],
        logger=lambda msg: None,
    )
    assert rc == 0
    assert Path(sample_image).read_bytes() == original_bytes
    assert out.exists()
    assert Path(sample_image) != out


# ===========================================================================
# 3. preview adds visible color difference (the polygon is actually drawn)
# ===========================================================================


def test_preview_draws_on_image(cli, sample_image, tmp_path):
    out = tmp_path / "preview.png"
    rc = cli.main(
        [
            "preview-roi",
            "--image", sample_image,
            "--polygon", "100,100;500,100;500,400;100,400",
            "--output", str(out),
        ],
        logger=lambda msg: None,
    )
    assert rc == 0
    with Image.open(out) as preview, Image.open(sample_image) as src:
        # Sample a pixel that is on the polygon edge — must differ from
        # the source grey background.
        assert preview.size == src.size
        assert preview.getpixel((100, 100)) != src.getpixel((100, 100))


# ===========================================================================
# 4. invalid polygon (too few points) returns non-zero, no output written
# ===========================================================================


def test_preview_invalid_polygon_no_output(cli, sample_image, tmp_path):
    out = tmp_path / "preview.png"
    rc = cli.main(
        [
            "preview-roi",
            "--image", sample_image,
            "--polygon", "100,100;500,100",  # only 2 points
            "--output", str(out),
        ],
        logger=lambda msg: None,
    )
    assert rc == 4
    assert not out.exists()


def test_preview_too_many_polygon_points_no_output(cli, sample_image, tmp_path):
    pts = ";".join(f"{i*10},{i*5}" for i in range(11))
    out = tmp_path / "preview.png"
    rc = cli.main(
        [
            "preview-roi",
            "--image", sample_image,
            "--polygon", pts,
            "--output", str(out),
        ],
        logger=lambda msg: None,
    )
    assert rc == 4
    assert not out.exists()


# ===========================================================================
# 5. missing image returns non-zero
# ===========================================================================


def test_preview_missing_image(cli, tmp_path):
    out = tmp_path / "preview.png"
    rc = cli.main(
        [
            "preview-roi",
            "--image", str(tmp_path / "does_not_exist.jpg"),
            "--polygon", "0,0;10,0;5,10",
            "--output", str(out),
        ],
        logger=lambda msg: None,
    )
    assert rc == 4
    assert not out.exists()


# ===========================================================================
# 6. six-vertex hexagonal polygon previews correctly
# ===========================================================================


def test_preview_hexagon(cli, sample_image, tmp_path):
    out = tmp_path / "hex_preview.jpg"
    rc = cli.main(
        [
            "preview-roi",
            "--image", sample_image,
            "--polygon",
            "400,100;600,200;600,400;400,500;200,400;200,200",
            "--zone-name", "hexagon_zone",
            "--output", str(out),
        ],
        logger=lambda msg: None,
    )
    assert rc == 0
    assert out.exists()


# ===========================================================================
# Bonus: line preview also works (two points)
# ===========================================================================


def test_preview_line_two_points(cli, sample_image, tmp_path):
    out = tmp_path / "line_preview.png"
    rc = cli.main(
        [
            "preview-roi",
            "--image", sample_image,
            "--line", "100,100;700,500",
            "--output", str(out),
        ],
        logger=lambda msg: None,
    )
    assert rc == 0
    assert out.exists()


def test_preview_neither_polygon_nor_line(cli, sample_image, tmp_path):
    out = tmp_path / "preview.png"
    rc = cli.main(
        [
            "preview-roi",
            "--image", sample_image,
            "--output", str(out),
        ],
        logger=lambda msg: None,
    )
    assert rc == 4
    assert not out.exists()
