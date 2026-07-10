"""C2.15A video-file-sink pressure log parser tests."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
PARSER = ROOT / "scripts" / "tools" / "parse_video_file_sink_pressure.py"


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "parse_video_file_sink_pressure",
        PARSER,
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_parse_video_file_sink_pressure_metrics() -> None:
    module = _load_module()

    text = "\n".join(
        [
            "New writer for source=replay-event-1 is initialized, amount of resident writers is 1",
            "New writer for source=replay-event-2 is initialized, amount of resident writers is 46",
            "pending reclaim=62",
            "The pipeline is about to stop. Operation took 0:00:22.913113.",
            "The pipeline is about to stop. Operation took 0:00:35.100000.",
            "The pipeline is about to stop. Operation took 0:00:40.000000.",
            "Received EOS from source replay-event-1",
            "Received EOS from source replay-event-2",
            "(python:1): GStreamer-WARNING **: transient warning",
            "(python:1): GStreamer-CRITICAL **: critical failure",
            "ERROR savant_rs::zeromq endpoint failed",
        ]
    )

    metrics = module.parse_video_file_sink_log(
        text,
        sink_instance="video-file-sink-a",
    )

    assert metrics["sink_instance"] == "video-file-sink-a"
    assert metrics["new_writer_count"] == 2
    assert metrics["resident_writer_max"] == 46
    assert metrics["pending_reclaim_max"] == 62
    assert metrics["pipeline_operation_count"] == 3
    assert round(metrics["pipeline_operation_duration_ms_p50"], 1) == 35100.0
    assert round(metrics["pipeline_operation_duration_ms_p95"], 1) == 39510.0
    assert round(metrics["pipeline_operation_duration_ms_p99"], 1) == 39902.0
    assert metrics["eos_count"] == 2
    assert metrics["gst_warning_count"] == 1
    assert metrics["gst_error_count"] == 2


def test_aggregate_video_file_sink_pressure_metrics() -> None:
    module = _load_module()

    aggregate = module.aggregate_video_file_sink_metrics(
        {
            "video-file-sink-a": {
                "new_writer_count": 46,
                "resident_writer_max": 46,
                "pending_reclaim_max": 12,
                "pipeline_operation_count": 46,
                "eos_count": 44,
                "gst_error_count": 1,
                "gst_warning_count": 3,
            },
            "video-file-sink-b": {
                "new_writer_count": 47,
                "resident_writer_max": 47,
                "pending_reclaim_max": 83,
                "pipeline_operation_count": 47,
                "eos_count": 45,
                "gst_error_count": 0,
                "gst_warning_count": 2,
            },
        }
    )

    assert aggregate == {
        "sink_instance_count": 2,
        "new_writer_count": 93,
        "resident_writer_max": 47,
        "pending_reclaim_max": 83,
        "pipeline_operation_count": 93,
        "eos_count": 89,
        "gst_error_count": 1,
        "gst_warning_count": 5,
    }
