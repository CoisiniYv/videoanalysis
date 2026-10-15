"""Publication rates use a measured log window, independently of retention."""

from __future__ import annotations

import json

import pytest

from test_midterm_pressure60_script import _config, _load_module


@pytest.fixture
def publication_case(tmp_path):
    module = _load_module()
    cfg = _config(module, artifact_dir=tmp_path, stream_count=2)
    window = {"started_at": "2026-10-15T01:00:00+00:00", "ended_at": "2026-10-15T01:02:00+00:00"}
    (tmp_path / "pressure_sampling_window.json").write_text(json.dumps(window), encoding="utf-8")
    (tmp_path / "rolling_cache_sinks_pressure.json").write_text(json.dumps({
        "observed_env": {"rolling-cache-sink": {"ROLLING_CACHE_RETENTION_SECONDS": "600", "ROLLING_CACHE_SEGMENT_SECONDS": "4"}},
    }), encoding="utf-8")
    (tmp_path / "runtime_log_capture.json").write_text(json.dumps({
        "since": "2026-10-15T00:59:00Z", "until": "2026-10-15T01:03:00Z",
        "captures": {"rolling_cache_sink_logs_since_start.txt": {"returncode": 0}},
    }), encoding="utf-8")
    lines = [
        "2026-10-15T00:59:59.000000000Z startup",
        "2026-10-15T00:59:59.900000000Z segment published source=pressure60_test_00 epoch=e session=s segment=before",
        "2026-10-15T01:00:00.000000000Z segment published source=pressure60_test_00 epoch=e session=s segment=1",
        "2026-10-15T01:00:04.000000000Z segment published source=pressure60_test_00 epoch=e session=s segment=2",
        "2026-10-15T01:00:08.000000000Z segment published source=pressure60_test_00 epoch=e session=s segment=3",
        "2026-10-15T01:00:12.000000000Z segment published source=pressure60_test_01 epoch=e session=s segment=4",
        "2026-10-15T01:01:59.000000000Z segment published source=pressure60_test_01 epoch=e session=s segment=5",
        "2026-10-15T01:02:00.000000000Z segment published source=pressure60_test_00 epoch=e session=s segment=after",
        "2026-10-15T01:02:01.000000000Z idle",
    ]
    path = tmp_path / "rolling_cache_sink_logs_since_start.txt"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return module, cfg, path


def test_publication_rates_count_each_source_in_explicit_half_open_window(publication_case):
    module, cfg, path = publication_case
    summary = module.summarize_rolling_cache_publications(cfg)
    assert summary["status"] == "available"
    assert summary["window"]["duration_s"] == 120
    assert summary["window"]["bounds"] == "[started_at, ended_at)"
    assert summary["actual_retention_seconds"] == 600
    assert summary["segment_seconds"] == 4
    assert summary["published_segments"] == 5
    assert summary["by_source"]["pressure60_test_00"]["actual_per_minute"] == 1.5
    assert summary["by_source"]["pressure60_test_01"]["actual_per_minute"] == 1.0
    assert summary["expected_per_source_minute"] == 15
    assert summary["by_source"]["pressure60_test_00"]["expected_segments_in_window"] == 30
    assert summary["count_basis"] == "timestamped_segment_published_logs"
    assert module.summarize_logs(cfg)["rolling_cache_publications"] == summary


def test_missing_capture_metadata_leaves_complete_rate_unknown(publication_case):
    module, cfg, path = publication_case
    (cfg.artifact_dir / "runtime_log_capture.json").unlink()
    summary = module.summarize_rolling_cache_publications(cfg)
    assert summary["status"] == "unknown"
    assert summary["observed_published_segments"] == 5
    assert summary["published_segments"] is None
    assert summary["by_source"]["pressure60_test_00"]["actual_per_minute"] is None


def test_partial_log_capture_does_not_claim_full_window_rate(publication_case):
    module, cfg, path = publication_case
    capture = cfg.artifact_dir / "runtime_log_capture.json"
    payload = json.loads(capture.read_text(encoding="utf-8"))
    payload["captures"][path.name]["returncode"] = 124
    capture.write_text(json.dumps(payload), encoding="utf-8")
    summary = module.summarize_rolling_cache_publications(cfg)
    assert summary["status"] == "unknown"
    assert any("capture_failed" in reason for reason in summary["unknown_reasons"])


def test_unqualified_local_log_times_are_unknown(publication_case):
    module, cfg, path = publication_case
    path.write_text("2026-10-15 09:00:01,000 segment published source=pressure60_test_00 segment=1\n", encoding="utf-8")
    summary = module.summarize_rolling_cache_publications(cfg)
    assert summary["status"] == "unknown"
    assert summary["unparsed_publication_lines"] == 1


def test_missing_log_file_does_not_turn_into_zero_publications(publication_case):
    module, cfg, path = publication_case
    path.unlink()
    summary = module.summarize_rolling_cache_publications(cfg)
    assert summary["status"] == "unknown"
    assert summary["published_segments"] is None


def test_missing_observed_retention_is_not_replaced_with_requested_value(publication_case):
    module, cfg, path = publication_case
    (cfg.artifact_dir / "rolling_cache_sinks_pressure.json").unlink()
    summary = module.summarize_rolling_cache_publications(cfg)
    assert summary["actual_retention_seconds"] is None
    assert summary["requested_retention_seconds"] == module.pressure_rolling_cache_retention_seconds(cfg)
    assert summary["expected_per_source_minute"] is None
