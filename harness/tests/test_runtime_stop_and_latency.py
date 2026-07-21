from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace


API_DIR = str(Path(__file__).resolve().parents[2] / "services" / "api")
if API_DIR in sys.path:
    sys.path.remove(API_DIR)
sys.path.insert(0, API_DIR)

api_root = Path(API_DIR).resolve()
loaded_app = sys.modules.get("app")
loaded_app_path = Path(getattr(loaded_app, "__file__", "") or "/").resolve()
if loaded_app is not None and not loaded_app_path.is_relative_to(api_root):
    for module_name in [
        name for name in list(sys.modules) if name == "app" or name.startswith("app.")
    ]:
        sys.modules.pop(module_name, None)

from app.services import runtime_control, runtime_latency  # noqa: E402


class _RowsCursor:
    def __init__(self, rows):
        self.rows = list(rows)
        self.executed: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql):
        self.executed.append(str(sql))

    def fetchone(self):
        return self.rows.pop(0)


class _Connection:
    def __init__(self, rows):
        self.cursor_value = _RowsCursor(rows)
        self.closed = False

    def cursor(self):
        return self.cursor_value

    def close(self):
        self.closed = True


def test_runtime_latency_uses_media_pts_not_database_write_age(monkeypatch) -> None:
    now_s = 2_000_000_000.0
    connection = _Connection(
        [
            (
                int((now_s - 100) * 1000),
                datetime.fromtimestamp(now_s - 1, tz=timezone.utc),
                1.0,
            ),
            (
                int((now_s - 200) * 1000),
                datetime.fromtimestamp(now_s - 2, tz=timezone.utc),
                2.0,
            ),
            (3, 1, datetime.fromtimestamp(now_s - 30, tz=timezone.utc)),
        ]
    )
    monkeypatch.setattr(
        runtime_latency,
        "get_settings",
        lambda: SimpleNamespace(database_url="postgresql://test"),
    )
    monkeypatch.setattr(runtime_latency.psycopg, "connect", lambda _url: connection)

    result = runtime_latency._database_latency(now_s)

    assert result["event_media_lag_s"] == 100.0
    assert result["event_write_age_s"] == 1.0
    assert result["bundle_event_lag_s"] == 200.0
    assert result["bundle_write_age_s"] == 2.0
    assert result["materialization_pending"] == 3
    assert result["materializing"] == 1
    assert result["oldest_active_task_age_s"] == 30.0
    assert "JOIN events e ON e.id = b.event_id" in connection.cursor_value.executed[1]
    assert connection.closed is True


def test_runtime_latency_annotation_separates_pts_lag_from_stream_write_age(
    monkeypatch,
) -> None:
    now_s = 2_000_000_000.0

    class _RedisClient:
        def xrevrange(self, _stream, count):
            assert count == 1
            return [
                (
                    b"2000000000000-0",
                    {
                        b"source_id": b"camera_00",
                        b"timestamp_ms": b"1999999900000",
                        b"data": b"{}",
                    },
                )
            ]

        def close(self):
            return None

    monkeypatch.setattr(
        runtime_latency.Redis,
        "from_url",
        lambda *_args, **_kwargs: _RedisClient(),
    )

    result = runtime_latency._latest_annotation(now_s)

    assert result["source_id"] == "camera_00"
    assert result["media_lag_s"] == 100.0
    assert result["annotation_write_age_s"] == 0.0


def test_dual_stop_stops_dynamic_sources_and_full_inference_chain(monkeypatch) -> None:
    actions: list[tuple[str, str]] = []
    fake_client = object()
    monkeypatch.setattr(
        runtime_control,
        "_discover_source_adapter_containers",
        lambda _client: ["video-analytics-source-camera_00", "video-analytics-source-camera_01"],
    )
    monkeypatch.setattr(
        runtime_control,
        "_container_action",
        lambda _client, name, action: (
            actions.append((name, action))
            or {"container": name, "action": action, "ok": True}
        ),
    )
    monkeypatch.setattr(runtime_control, "_disable_all_cameras", lambda: 40)
    monkeypatch.setattr(
        runtime_control,
        "runtime_control_status",
        lambda **_kwargs: {"dual": [], "management": []},
    )

    result = runtime_control.stop_dual_runtime(docker_client=fake_client)

    assert result["disabled_camera_count"] == 40
    assert result["source_containers_stopped"] == [
        "video-analytics-source-camera_00",
        "video-analytics-source-camera_01",
    ]
    assert actions[:2] == [
        ("video-analytics-source-camera_00", "stop"),
        ("video-analytics-source-camera_01", "stop"),
    ]
    stopped_names = {name for name, action in actions if action == "stop"}
    assert "video-analytics-midterm-replay-raw-fanout-a" in stopped_names
    assert "video-analytics-midterm-replay-raw-fanout-b" in stopped_names
    assert "video-analytics-midterm-savant-a" in stopped_names
    assert "video-analytics-midterm-savant-b" in stopped_names
    assert "video-analytics-midterm-adaface-roi-worker" in stopped_names
    assert "video-analytics-midterm-cuda-mps-operator" in stopped_names
    assert "video-analytics-midterm-event-worker" in result["drain_workers_left_running"]
    assert "video-analytics-midterm-media-worker" in result["drain_workers_left_running"]
