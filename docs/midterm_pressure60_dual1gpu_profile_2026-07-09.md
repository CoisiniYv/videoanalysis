# Midterm Pressure60 Single-GPU Dual-Branch Profile - 2026-07-09

This is the canonical profile for the current 60-stream pressure methodology.
Do not rerun or compare pressure results without explicitly preserving the
topology and batch settings below.

## Profile Names

| Profile | Purpose | FPS |
| --- | --- | --- |
| `8fps-stress` | Current stress/validation profile used on 2026-07-09 | `8/1` |
| `4fps-t4` | Production-T4 default probe, same topology with lower analysis load | `4/1` |

The two profiles differ only by `--fps` and run-id prefix. The topology,
batching, cooldown, duration, drain, and evidence mode must stay the same.

## Required Topology

This profile is single-card dual-branch inference:

- two Savant branches: `savant-a` and `savant-b`;
- two analysis forwarders: `analysis-forwarder-a` and `analysis-forwarder-b`;
- both Savant branches pinned to the same physical GPU, normally GPU `0`;
- source placement is balanced `30+30`;
- evidence is split across 4 Replay/video-file-sink shards;
- rolling-cache evidence materialization is enabled;
- 8090 visual evidence is DB-backed: timeline rows and overlay rows must be
  written, not only read from sidecar fallback files.

Required flags:

```text
--dual-shard-same-gpu
--dual-shard-gpu 0
--dual-shard-source-mode balanced
--evidence-shard-count 4
--rolling-cache-evidence
```

Do not replace this with a single Savant branch when discussing this profile.
Do not omit `--dual-shard-same-gpu`.

## Required Pressure Parameters

```text
--streams 60
--min-fps 1/1
--duration-s 400
--sample-interval-s 30
--drain-s 120
--guard-wait-s 1200
--keep-evidence -1
--evidence-group-size 20
--evidence-policy-groups 5:5,10:10,15:15
--batch-size 4
--pose-batch-size 4
--face-detector-batch-size 4
--face-embedding-batch-size 16
--max-parallel-streams 64
--batched-push-timeout 40000
--pressure-algorithm-cooldown-s 60
--force-runtime-restart
--rolling-cache-prefill-s 25
--pressure-source-visibility-timeout-s 300
--pressure-source-visibility-stable-samples 2
--pressure-source-visibility-restart-attempts 1
--pressure-source-ffmpeg-timeout-ms 60000
--pressure-source-start-stagger-s 0.5
```

YOLO26-pose and YOLOv8-Face use dynamic-batch ONNX variants generated from the
fixed-batch exported assets, so batch 4 must produce four output slices, not a
single batch-1 tensor.

Before running this profile on a fresh machine, generate the dynamic ONNX files
and TensorRT engines:

```bash
bash scripts/tools/build_yolo_dynamic_batch_engines.sh
```

The cooldown must be explicit. The current script default has drifted before;
do not rely on the default for this profile.

## Canonical Commands

Prefer the wrapper so the profile name is visible in shell history:

```bash
bash scripts/runtime/run_pressure60_dual1gpu_profile.sh 8fps-stress
```

For the production T4 4 FPS probe:

```bash
bash scripts/runtime/run_pressure60_dual1gpu_profile.sh 4fps-t4
```

Equivalent direct command for `8fps-stress`:

```bash
python scripts/runtime/run_midterm_pressure60.py \
  --run-id pressure60_8p1_dual1gpu_cd60_$(date -u +%Y%m%dT%H%M%SZ) \
  --streams 60 \
  --fps 8/1 \
  --min-fps 1/1 \
  --duration-s 400 \
  --sample-interval-s 30 \
  --drain-s 120 \
  --guard-wait-s 1200 \
  --keep-evidence -1 \
  --evidence-group-size 20 \
  --evidence-policy-groups 5:5,10:10,15:15 \
  --batch-size 4 \
  --pose-batch-size 4 \
  --face-detector-batch-size 4 \
  --face-embedding-batch-size 16 \
  --max-parallel-streams 64 \
  --batched-push-timeout 40000 \
  --pressure-algorithm-cooldown-s 60 \
  --force-runtime-restart \
  --dual-shard-same-gpu \
  --dual-shard-gpu 0 \
  --dual-shard-source-mode balanced \
  --evidence-shard-count 4 \
  --rolling-cache-evidence \
  --rolling-cache-prefill-s 25 \
  --pressure-source-visibility-timeout-s 300 \
  --pressure-source-visibility-poll-s 5 \
  --pressure-source-visibility-stable-samples 2 \
  --pressure-source-visibility-restart-attempts 1 \
  --pressure-source-ffmpeg-timeout-ms 60000 \
  --pressure-source-start-stagger-s 0.5
```

For `4fps-t4`, change only:

```text
--run-id pressure60_4p1_dual1gpu_cd60_<UTC timestamp>
--fps 4/1
```

## Reference Run

The current reference run is:

```text
run_id=pressure60_8p1_dual1gpu_cd60_20260709T040138Z
artifact=/data/video-analytics/artifacts/pressure60_8p1_dual1gpu_cd60_20260709T040138Z
status=failed_pressure_gates
```

Interpretation:

- throughput result: mostly successful;
- official gate result: not passed;
- visual evidence result: not accepted;
- main improvements over the previous single-branch run: queue did not fill,
  forwarded target ratio reached the 8 FPS target, and retained evidence
  materialized cleanly.
- evidence-window mistake found after review: this run used
  `2:2,3:3,5:5,8:8,10:10,15:15`; it is not the fixed `5:5,10:10,15:15`
  acceptance profile.
- 8090 overlay/timeline issue found after review: DB expanded rows were disabled,
  so annotations relied on filesystem fallback and sink timeline rows were absent
  from the DB-backed API path.

Key measurements from the reference run:

```text
max_forwarder_sources=60
max_savant_sources=60
max_queue_depth=1
queue_full_samples=0
forwarded_frames=192260
target_forwarded_frames=192000
forwarded_target_ratio=1.0014
savant_send_failures=2
validate_seq_iq=148062
retained_events=243
evidence_bundles=243
evidence_tasks=243 materialized
8090 evidence checked=243/243
8090 video annotations checked=173/173
8090 zero-annotation image evidence skipped=70
evidence_overlay_segments=0
evidence_frame_timeline=0
rolling_cache_source_count=60
```

## Acceptance Rules

Treat a run as production-credible only if the report records:

- the exact profile name, run id, artifact path, and git/dirty state;
- both branches are present and pinned to the intended GPU;
- 60 sources are visible in both Savant and forwarder;
- `queue_full_samples=0`;
- `max_queue_depth` remains small and does not trend upward;
- forwarded target ratio is close to 1.0 for the selected FPS;
- `savant_send_failures=0`;
- Redis consumer lag for core groups is 0 at drain;
- retained evidence bundles are playable/queryable in 8090;
- materialization backlog drains to 0;
- annotation checks distinguish video evidence from expected zero-annotation
  face image evidence;
- every video evidence policy is one of `5:5`, `10:10`, or `15:15`;
- every video raw clip duration is within tolerance of `10s`, `20s`, or `30s`;
- 8090 annotations are served from `database_overlay_segments`, not filesystem
  fallback;
- 8090 sink metadata is served from DB timeline rows and has nonzero frame rows;
- intrusion video evidence has displayable bbox objects and person context, so
  boxes and track trails are visible in the operator page.

For now, interpret `validate_seq_iq` separately from queue/send failures. In
this topology the forwarder samples from roughly 24-26 FPS down to the selected
analysis FPS, so sequence gaps can include expected sampler behavior. A final
gate should distinguish expected sampling gaps from real message loss.

## Known Caveat

The 2026-07-09 reference run passed
`--pressure-source-ffmpeg-timeout-ms 60000`, and `run_config.json` recorded it,
but source start logs still showed `FFMPEG_TIMEOUT_MS=20000`. This did not block
that run, because all sources became visible quickly, but the next hardening pass
should fix or explicitly verify timeout propagation.
