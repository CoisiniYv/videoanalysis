"""Per-shard queue capacity and drop-metric reporting contracts."""

from __future__ import annotations

import json

from test_midterm_pressure60_script import _config, _load_module


def test_forwarder_parser_keeps_stale_resync_and_oldest_frame_age():
    module = _load_module()
    parsed = module.parse_forwarder_metrics_text(
        'va_forwarder_queue_depth 12\n'
        'va_forwarder_queue_head_age_ms 3210\n'
        'va_forwarder_frames_stale_dropped_total{source_id="camera-a"} 17\n'
        'va_forwarder_frames_gop_resync_dropped_total{source_id="camera-a"} 23\n'
    )
    assert parsed["global"]["queue_head_age_ms"] == 3210
    assert parsed["sources"][0]["frames_stale_dropped_total"] == 17
    assert parsed["sources"][0]["frames_gop_resync_dropped_total"] == 23


def test_queue_capacity_readback_uses_each_active_shards_environment(monkeypatch):
    module = _load_module()
    cfg = _config(module, dual_shard_same_gpu=True, rolling_cache_evidence=True)
    observed = []

    def env(name):
        observed.append(name)
        return {"FORWARDER_QUEUE_MAX_SIZE": "8192" if name.endswith("-a") else "4096"}

    monkeypatch.setattr(module, "docker_container_env", env)
    overview = {"forwarder": {"shards": [
        {"shard_id": "replay-a", "global": {"queue_depth": 1}},
        {"shard_id": "replay-b", "global": {"queue_depth": 2}},
    ]}}
    module.record_forwarder_queue_capacities(cfg, overview)
    assert observed == ["video-analytics-midterm-replay-raw-fanout-a", "video-analytics-midterm-replay-raw-fanout-b"]
    assert [shard["global"]["queue_max_size"] for shard in overview["forwarder"]["shards"]] == [8192, 4096]


def _summary(tmp_path, depths, capacities):
    module = _load_module()
    samples = tmp_path / "samples"
    samples.mkdir()
    shards = [
        {"shard_id": shard, "global": {"queue_depth": depth, "queue_max_size": capacity}}
        for shard, depth, capacity in zip(("replay-a", "replay-b"), depths, capacities)
    ]
    (samples / "runtime_000.json").write_text(json.dumps({
        "forwarder": {"global": {"queue_depth": sum(depths)}, "shards": shards},
    }), encoding="utf-8")
    return module.summarize_runtime_samples(_config(module, artifact_dir=tmp_path))


def test_two_nonfull_shards_do_not_become_full_at_legacy_sum_threshold(tmp_path):
    summary = _summary(tmp_path, [1500, 1500], [8192, 8192])
    assert summary["queue_full_samples"] == 0
    assert summary["legacy_queue_full_samples"] == 1
    assert summary["queue_full_samples_by_shard"] == {}


def test_queue_fullness_uses_each_shards_own_capacity(tmp_path):
    summary = _summary(tmp_path, [512, 7600], [512, 8192])
    assert summary["queue_full_samples"] == 1
    assert summary["queue_full_samples_by_shard"] == {"replay-a": 1}


def test_missing_queue_capacity_is_explicit_and_does_not_invent_2048(tmp_path):
    summary = _summary(tmp_path, [2048, 0], [None, 8192])
    assert summary["queue_full_samples"] == 0
    assert summary["legacy_queue_full_samples"] == 1
    assert summary["queue_capacity_unknown_samples"] == 1
