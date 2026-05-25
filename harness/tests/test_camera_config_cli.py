"""Tests for the operator camera config CLI (Phase C1.3)."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest


SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "camera_config_cli.py"


def _load_script_module():
    spec = importlib.util.spec_from_file_location(
        "camera_config_cli_under_test", SCRIPT_PATH
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def cli():
    return _load_script_module()


# ---------------------------------------------------------------------------
# Fake HTTP client. Captures calls and returns programmable (status, body).
# ---------------------------------------------------------------------------


class _FakeHttp:
    def __init__(
        self,
        *,
        status: int = 200,
        parsed: Any = None,
        raw: str = "",
    ):
        self.status = status
        self.parsed = parsed if parsed is not None else {"data": {}, "error": None}
        self.raw = raw or json.dumps(self.parsed)
        self.calls: List[Tuple[str, str, Optional[Dict[str, Any]]]] = []

    def __call__(self, method: str, url: str, payload: Optional[Dict[str, Any]] = None,
                 *, timeout: float = 0) -> Tuple[int, Any, str]:
        self.calls.append((method, url, payload))
        return self.status, self.parsed, self.raw


# ===========================================================================
# parse_polygon_points / parse_line_points
# ===========================================================================


def test_parse_polygon_six_points(cli):
    text = "100,120;600,80;1200,160;1700,500;1300,960;250,860"
    pts = cli.parse_polygon_points(text)
    assert pts == [
        [100.0, 120.0],
        [600.0, 80.0],
        [1200.0, 160.0],
        [1700.0, 500.0],
        [1300.0, 960.0],
        [250.0, 860.0],
    ]


def test_parse_polygon_three_points_minimum(cli):
    assert cli.parse_polygon_points("0,0;10,0;5,10") == [[0.0, 0.0], [10.0, 0.0], [5.0, 10.0]]


def test_parse_polygon_too_few_points_raises(cli):
    with pytest.raises(ValueError) as exc:
        cli.parse_polygon_points("0,0;10,0")
    assert "3 to 10" in str(exc.value)


def test_parse_polygon_too_many_points_raises(cli):
    text = ";".join(f"{i},{i}" for i in range(11))
    with pytest.raises(ValueError) as exc:
        cli.parse_polygon_points(text)
    assert "3 to 10" in str(exc.value)


def test_parse_polygon_non_numeric_raises(cli):
    with pytest.raises(ValueError) as exc:
        cli.parse_polygon_points("0,0;10,abc;5,10")
    assert "non-numeric" in str(exc.value)


def test_parse_polygon_bad_pair_shape_raises(cli):
    with pytest.raises(ValueError):
        cli.parse_polygon_points("0,0;10;5,10")


def test_parse_polygon_empty_raises(cli):
    with pytest.raises(ValueError):
        cli.parse_polygon_points("")
    with pytest.raises(ValueError):
        cli.parse_polygon_points("   ")


def test_parse_line_two_points(cli):
    assert cli.parse_line_points("100,100;900,900") == [[100.0, 100.0], [900.0, 900.0]]


def test_parse_line_requires_exactly_two(cli):
    with pytest.raises(ValueError) as exc:
        cli.parse_line_points("0,0;5,5;10,10")
    assert "exactly 2" in str(exc.value)


def test_parse_polygon_accepts_floats(cli):
    pts = cli.parse_polygon_points("1.5,2.5;3.0,4.0;5,6")
    assert pts == [[1.5, 2.5], [3.0, 4.0], [5.0, 6.0]]


# ===========================================================================
# add-camera
# ===========================================================================


def _ok_envelope(data: Any) -> Dict[str, Any]:
    return {"data": data, "error": None, "request_id": "test"}


def test_add_camera_payload(cli):
    http = _FakeHttp(status=200, parsed=_ok_envelope({"id": "cam_001"}))
    logs: List[str] = []
    rc = cli.main(
        [
            "add-camera",
            "--api-base-url", "http://api:8004",
            "--camera-id", "cam_001",
            "--source-id", "cam_001_src",
            "--name", "测试摄像头",
            "--location", "测试区域",
            "--uri", "rtsp://user:secret@cam.local:554/stream",
            "--site-id", "site_a",
            "--gpu-id", "0",
            "--enabled", "true",
        ],
        http=http,
        logger=logs.append,
    )
    assert rc == 0, logs
    method, url, payload = http.calls[0]
    assert method == "POST"
    assert url == "http://api:8004/api/v1/cameras"
    assert payload["id"] == "cam_001"
    assert payload["source_id"] == "cam_001_src"
    assert payload["name"] == "测试摄像头"
    assert payload["location"] == "测试区域"
    assert payload["site_id"] == "site_a"
    assert payload["rtsp_url"] == "rtsp://user:secret@cam.local:554/stream"
    assert payload["gpu_id"] == 0
    assert payload["enabled"] is True

    # Logs MUST NOT contain the RTSP URL.
    joined = "\n".join(logs)
    assert "rtsp://" not in joined
    assert "user:secret" not in joined
    assert "cam.local" not in joined
    # Safe identifiers ARE logged.
    assert "cam_001" in joined
    assert "cam_001_src" in joined


def test_add_camera_409_returns_nonzero(cli):
    http = _FakeHttp(
        status=409,
        parsed={"data": None, "error": {"message": "camera already exists", "code": 409}},
    )
    rc = cli.main(
        [
            "add-camera",
            "--api-base-url", "http://api:8004",
            "--camera-id", "cam_001",
            "--source-id", "src",
            "--name", "N",
            "--uri", "file:///tmp/x.mp4",
        ],
        http=http,
        logger=lambda msg: None,
    )
    assert rc == 3


# ===========================================================================
# add-zone
# ===========================================================================


def test_add_zone_polygon_payload(cli):
    http = _FakeHttp(status=200, parsed=_ok_envelope({"zone_name": "perimeter"}))
    rc = cli.main(
        [
            "add-zone",
            "--api-base-url", "http://api:8004",
            "--camera-id", "cam_001",
            "--zone-name", "perimeter",
            "--polygon", "100,120;600,80;1200,160;1700,500;1300,960;250,860",
        ],
        http=http,
        logger=lambda msg: None,
    )
    assert rc == 0
    method, url, payload = http.calls[0]
    assert url == "http://api:8004/api/v1/cameras/cam_001/zones"
    assert payload["zone_type"] == "polygon"
    assert payload["zone_name"] == "perimeter"
    assert len(payload["points"]) == 6
    assert payload["points"][0] == [100.0, 120.0]


def test_add_zone_line_payload(cli):
    http = _FakeHttp(status=200, parsed=_ok_envelope({"zone_name": "tw"}))
    rc = cli.main(
        [
            "add-zone",
            "--api-base-url", "http://api:8004",
            "--camera-id", "cam_001",
            "--zone-name", "tw",
            "--line", "0,0;100,100",
            "--zone-type", "direction_line",
        ],
        http=http,
        logger=lambda msg: None,
    )
    assert rc == 0
    _, _, payload = http.calls[0]
    assert payload["zone_type"] == "direction_line"
    assert payload["points"] == [[0.0, 0.0], [100.0, 100.0]]


def test_add_zone_rejects_both_polygon_and_line(cli):
    http = _FakeHttp()
    rc = cli.main(
        [
            "add-zone",
            "--camera-id", "cam_001",
            "--zone-name", "z",
            "--polygon", "0,0;1,0;1,1",
            "--line", "0,0;10,10",
        ],
        http=http,
        logger=lambda msg: None,
    )
    assert rc != 0
    assert http.calls == []


def test_add_zone_rejects_neither(cli):
    http = _FakeHttp()
    rc = cli.main(
        [
            "add-zone",
            "--camera-id", "cam_001",
            "--zone-name", "z",
        ],
        http=http,
        logger=lambda msg: None,
    )
    assert rc != 0
    assert http.calls == []


def test_add_zone_invalid_polygon_does_not_call_api(cli):
    http = _FakeHttp()
    rc = cli.main(
        [
            "add-zone",
            "--camera-id", "cam_001",
            "--zone-name", "perimeter",
            "--polygon", "0,0;10,0",  # only 2 points
        ],
        http=http,
        logger=lambda msg: None,
    )
    assert rc == 4
    assert http.calls == []


# ===========================================================================
# add-intrusion-rule
# ===========================================================================


def test_add_intrusion_rule_payload(cli):
    http = _FakeHttp(status=200, parsed=_ok_envelope({"rule_type": "intrusion"}))
    rc = cli.main(
        [
            "add-intrusion-rule",
            "--api-base-url", "http://api:8004",
            "--camera-id", "cam_001",
            "--zone", "perimeter",
            "--min-inside-ms", "1000",
            "--cooldown-s", "30",
            "--severity", "medium",
            "--snapshot-required", "true",
            "--clip-required", "true",
        ],
        http=http,
        logger=lambda msg: None,
    )
    assert rc == 0
    method, url, payload = http.calls[0]
    assert url == "http://api:8004/api/v1/cameras/cam_001/rules"
    assert payload["rule_type"] == "intrusion"
    cfg = payload["config"]
    assert cfg["zone"] == "perimeter"
    assert cfg["min_inside_ms"] == 1000
    assert cfg["cooldown_s"] == 30
    assert cfg["severity"] == "medium"
    assert cfg["snapshot_required"] is True
    assert cfg["clip_required"] is True


def test_add_intrusion_rule_rejects_invalid_min_inside_ms(cli):
    http = _FakeHttp()
    rc = cli.main(
        [
            "add-intrusion-rule",
            "--camera-id", "cam_001",
            "--zone", "perimeter",
            "--min-inside-ms", "0",
        ],
        http=http,
        logger=lambda msg: None,
    )
    assert rc != 0
    assert http.calls == []


# ===========================================================================
# show-camera
# ===========================================================================


def test_show_camera_calls_config_endpoint_and_redacts_rtsp(cli):
    http = _FakeHttp(
        status=200,
        parsed=_ok_envelope({
            "camera": {
                "id": "cam_001",
                "rtsp_url": "rtsp://user:secret@cam.local/stream",
                "source_id": "src",
                "name": "T",
                "enabled": True,
                "gpu_id": 0,
            },
            "zones": [],
            "rules": [],
        }),
    )
    logs: List[str] = []
    rc = cli.main(
        [
            "show-camera",
            "--api-base-url", "http://api:8004",
            "--camera-id", "cam_001",
        ],
        http=http,
        logger=logs.append,
    )
    assert rc == 0
    method, url, payload = http.calls[0]
    assert method == "GET"
    assert url == "http://api:8004/api/v1/cameras/cam_001/config"
    joined = "\n".join(logs)
    assert "user:secret" not in joined
    assert "cam.local" not in joined
    assert "<rtsp-redacted>" in joined or "<redacted>" in joined


def test_show_camera_404_returns_nonzero(cli):
    http = _FakeHttp(
        status=404,
        parsed={"data": None, "error": {"message": "camera not found", "code": 404}},
    )
    rc = cli.main(
        [
            "show-camera",
            "--camera-id", "ghost",
        ],
        http=http,
        logger=lambda msg: None,
    )
    assert rc == 2


# ===========================================================================
# export-runtime / start-source / stop-source delegation
# ===========================================================================


def test_export_runtime_delegates(cli, monkeypatch, tmp_path):
    """export-runtime forwards args to scripts/config/export_runtime_configs.py."""
    captured: Dict[str, Any] = {}

    class _FakeExportMod:
        @staticmethod
        def main(argv, *, logger=None):
            captured["argv"] = list(argv)
            return 0

    monkeypatch.setattr(cli, "_import_script", lambda name: _FakeExportMod())

    rc = cli.main(
        [
            "export-runtime",
            "--api-base-url", "http://api:8004",
            "--module-config-output", str(tmp_path / "cam.yml"),
            "--sources-output", str(tmp_path / "src.yml"),
            "--include-disabled",
        ],
        logger=lambda msg: None,
    )
    assert rc == 0
    argv = captured["argv"]
    assert "--api-base-url" in argv
    assert "--module-config-output" in argv
    assert "--include-disabled" in argv


def test_start_source_delegates(cli, monkeypatch):
    captured: Dict[str, Any] = {}

    class _FakeCtrl:
        @staticmethod
        def main(argv, *, logger=None):
            captured["argv"] = list(argv)
            return 0

    monkeypatch.setattr(cli, "_import_script", lambda name: _FakeCtrl())
    rc = cli.main(
        [
            "start-source",
            "--source-id", "src_001",
            "--sources", "infra/generated/sources.generated.yml",
            "--network", "c1-official-adapter_default",
        ],
        logger=lambda msg: None,
    )
    assert rc == 0
    argv = captured["argv"]
    assert argv[0] == "start"
    assert "--source-id" in argv
    assert "src_001" in argv


def test_stop_source_delegates(cli, monkeypatch):
    captured: Dict[str, Any] = {}

    class _FakeCtrl:
        @staticmethod
        def main(argv, *, logger=None):
            captured["argv"] = list(argv)
            return 0

    monkeypatch.setattr(cli, "_import_script", lambda name: _FakeCtrl())
    rc = cli.main(
        ["stop-source", "--source-id", "src_001"],
        logger=lambda msg: None,
    )
    assert rc == 0
    assert captured["argv"] == ["stop", "--source-id", "src_001"]


def test_delegate_propagates_nonzero(cli, monkeypatch):
    class _FakeMod:
        @staticmethod
        def main(argv, *, logger=None):
            return 4

    monkeypatch.setattr(cli, "_import_script", lambda name: _FakeMod())
    rc = cli.main(
        ["stop-source", "--source-id", "src_001"],
        logger=lambda msg: None,
    )
    assert rc == 4
