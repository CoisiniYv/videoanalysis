# Midterm Analysis-forwarder 30-stream Offline Pressure Record - 2026-06-26

## Scope

This record captures an offline one-branch pressure run for
`services/analysis-forwarder`.

The test does not connect to real RTSP cameras, Replay RocksDB, Savant
DeepStream inference, Redis, PostgreSQL, or GPU devices. It isolates the
forwarder hop:

```text
synthetic Replay writer -> AnalysisForwarder -> synthetic Savant sink
```

The goal is to answer whether one analysis-forwarder branch can sustain a
30-source encoded-payload forwarding workload before a full 30-stream runtime
pressure test.

## Command

Run time: `2026-06-26 19:49:54 CST`.

```bash
scripts/spikes/check_analysis_forwarder_branch_pressure.py
```

The script re-executed itself in the Savant DeepStream image so the probe used
the same `savant_rs` package family as the runtime forwarder:

```text
ghcr.io/insight-platform/savant-deepstream:0.6.0-7.1
```

Report artifact:

```text
/data/video-analytics/artifacts/analysis_forwarder_pressure/analysis_forwarder_branch_pressure_20260626T194954.json
```

## Workload

```text
streams: 30
input_fps_per_stream: 30
duration_s: 60
payload_bytes_per_frame: 25000
analysis_fps: 8/1
queue_max_size: 256
receive_hwm: 1000
send_hwm: 50
send_timeout_ms: 100
send_retries: 0
keyframe_interval: 30
```

Approximate synthetic input pressure:

```text
30 streams * 30 fps = 900 messages/s
900 messages/s * 25000 bytes ~= 22.5 MB/s encoded payload input
```

## Result

Result: pass.

```text
PASS_ANALYSIS_FORWARDER_BRANCH_30_STREAM_PRESSURE
```

Key counters:

| Metric | Value |
| --- | ---: |
| expected_sent | 54000 |
| send_success | 54000 |
| forwarder_seen_total | 54000 |
| forwarder_forwarded_total | 14400 |
| forwarder_dropped_total | 39600 |
| forwarder_send_failures_total | 0 |
| sink_received_total | 14400 |
| input_fps_actual | 900.013 |
| forwarded_fps_total | 240.003 |
| forwarded_fps_per_source_min | 8.0 |
| forwarded_fps_per_source_max | 8.0 |
| per_source_forwarded_min | 480 |
| per_source_forwarded_max | 480 |
| max_queue_depth | 1 |
| max_schedule_lag_ms | 7.397 |
| process_cpu_ratio | 0.412 |
| ru_maxrss_kb | 60752 |
| tracemalloc_peak_bytes | 7444393 |

Forward latency measured from synthetic writer send timestamp to synthetic sink
receive timestamp:

| Latency | Value |
| --- | ---: |
| count | 14400 |
| min_ms | 0.095 |
| p50_ms | 0.534 |
| mean_ms | 0.504 |
| p95_ms | 0.868 |
| p99_ms | 1.322 |
| max_ms | 4.545 |

Acceptance checks from the report:

| Check | Result | Detail |
| --- | --- | --- |
| source_generator_sent_all_frames | pass | send_success=54000 expected=54000 |
| forwarder_saw_all_successful_frames | pass | seen=54000 send_success=54000 |
| forwarder_send_failures_within_limit | pass | forwarder_send_failures=0 limit=0 |
| sink_received_every_forwarded_frame | pass | sink_received=14400 forwarded=14400 |
| queue_depth_within_limit | pass | max_queue_depth=1 limit=64 |
| per_source_forwarding_fair | pass | skew_ratio=0.0000 limit=0.2000 |
| per_source_forwarded_fps_above_min | pass | min_forwarded_fps=8.0 limit=6.75 |
| p95_forward_latency_within_limit | pass | p95_latency_ms=0.868 limit=1000.0 |
| source_generator_kept_up | pass | send_elapsed_s=59.999 expected=60.000 overrun_limit=0.200 |

## Interpretation

For this isolated forwarder workload, one branch sustained 30 synthetic encoded
streams at the intended input rate. The forwarder admitted exactly 8 FPS per
source, dropped the rest in the analysis branch, preserved per-source fairness,
and did not report any Savant-side send failures. Queue pressure stayed low
(`max_queue_depth=1`).

This supports the local conclusion that the current forwarder implementation is
not the obvious first bottleneck for one 30-stream branch at approximately
22.5 MB/s of encoded payload input.

## Limits

This is not a full production 30-stream or 60-stream readiness result.

Not covered by this offline probe:

- RTSP adapter CPU/network behavior.
- Replay RocksDB write/read/TTL compaction behavior.
- Real Replay job routing.
- Savant decode, DeepStream batching, GPU inference, tracker, and pyfunc cost.
- Redis stream sizing and downstream workers.
- Evidence materialization bursts.
- Cross-shard routing for the 30x2 topology.

The remaining production gate is still a staged runtime pressure test with real
source adapters, Replay, Savant, Redis, workers, and evidence flow enabled.
