# 20_dual_4090_as_t4_two_source_validation.md

## 1. Goal

Validate the production sharding topology on the current development host by
treating the two NVIDIA RTX 4090 GPUs as stand-ins for the future two T4 GPUs.

This is a topology and evidence correctness validation, not a 60-stream capacity
claim. The expected runtime target for this spec is:

```text
RTSP source A
  -> replay-a
  -> analysis-forwarder-a
  -> savant-a on GPU 0
  -> Redis/Postgres events and annotations
  -> clip-worker replay job back to replay-a
  -> video-file-sink-a
  -> media-worker evidence bundle

RTSP source B
  -> replay-b
  -> analysis-forwarder-b
  -> savant-b on GPU 1
  -> Redis/Postgres events and annotations
  -> clip-worker replay job back to replay-b
  -> video-file-sink-b
  -> media-worker evidence bundle
```

The final proof must show that two real RTSP sources can run simultaneously
through two independent Replay + forwarder + Savant inference chains and still
produce verified evidence bundles.

## 2. Relationship To Spec 16

`specs/16_dual_path_30x2_t4_production_optimization.md` remains the production
target:

- 2 T4 GPUs.
- 60 total RTSP streams.
- 30 streams per Replay/forwarder/Savant shard.
- staged pressure tests from 10 to 30 to 60 streams.

This spec is the smaller validation step for the current machine:

- 2 RTX 4090 GPUs.
- 2 real RTSP sources.
- 1 source per shard.
- no 60-stream throughput acceptance.

The architecture should stay the same as the T4 plan: two self-contained shards
split by `source_id`. The only expected hardware-specific difference is TensorRT
engine generation. Engines built for 4090 are not evidence that T4 engines will
work without regeneration, and T4 engines must be regenerated or validated on
the T4 target later.

## 3. Current Baseline

Current running midterm topology is still single-shard:

```text
RTSP sources -> replay-service -> analysis-forwarder -> savant-security
             -> clip-worker -> video-file-sink -> media-worker
```

Existing partial shard work:

- `infra/config/replay-shards.midterm.json` defines replay shard records.
- `infra/docker-compose.midterm.yml` has `dual-replay-shards` services for
  `replay-a`, `replay-b`, `video-file-sink-a`, and `video-file-sink-b`.
- `clip-worker` has shard-routing support through `REPLAY_SHARDS_CONFIG_PATH`.
- A prior looped-source check proved replay job routing to `replay-a` and
  `replay-b`, but did not prove full post-Savant frame proof or verified
  evidence readiness.

Missing for this spec:

- `analysis-forwarder-a` and `analysis-forwarder-b`.
- `savant-a` and `savant-b`.
- two Replay configs with distinct `out_stream.url` targets.
- two real source assignments to `replay-a` and `replay-b`.
- runtime validation that both inference modules emit annotations and both
  shards produce verified evidence bundles.

## 4. Implementation Plan

### Phase 1 - Shard Config For Two Real Sources

Create a two-source shard config for the current host. It can reuse
`infra/config/replay-shards.midterm.json` only if that file is intentionally
converted from the synthetic 60-source map to the current two real sources.
Otherwise create a dedicated file such as:

```text
infra/config/replay-shards.dual_4090_two_source.json
```

The config must map the two enabled RTSP `source_id` values exactly:

```json
{
  "default_shard_id": "replay-a",
  "shards": [
    {
      "shard_id": "replay-a",
      "replay_api_url": "http://replay-a:8080",
      "in_stream_endpoint": "dealer+connect:tcp://replay-a:5555",
      "replay_job_sink_url": "dealer+connect:tcp://video-file-sink-a:6666",
      "source_ids": ["<source-a-id>"]
    },
    {
      "shard_id": "replay-b",
      "replay_api_url": "http://replay-b:8080",
      "in_stream_endpoint": "dealer+connect:tcp://replay-b:5555",
      "replay_job_sink_url": "dealer+connect:tcp://video-file-sink-b:6666",
      "source_ids": ["<source-b-id>"]
    }
  ]
}
```

Acceptance:

- every enabled validation source resolves to exactly one shard;
- no fallback to the legacy `replay-service` route during validation;
- unknown source ids fail closed for replay job creation.

### Phase 2 - Replay Config Split

Create two Replay config files from `modules/savant_replay/config.midterm.json`:

```text
modules/savant_replay/config.midterm.replay-a.json
modules/savant_replay/config.midterm.replay-b.json
```

The only required difference is `out_stream.url`:

```text
replay-a -> dealer+connect:tcp://replay-raw-fanout-a:5557
replay-b -> dealer+connect:tcp://replay-raw-fanout-b:5557
```

Acceptance:

- compose mounts the correct config into each Replay shard;
- `replay-a` never forwards to `analysis-forwarder-b`;
- `replay-b` never forwards to `analysis-forwarder-a`.

### Phase 3 - Dual 4090 Compose Profile

Add a dedicated profile, for example:

```text
dual-4090-two-source
```

This profile must include:

- `replay-a`
- `replay-b`
- `analysis-forwarder-a`
- `analysis-forwarder-b`
- `savant-a`
- `savant-b`
- `video-file-sink-a`
- `video-file-sink-b`

The original single-shard services should remain available for rollback.

GPU assignment:

```text
savant-a -> NVIDIA_VISIBLE_DEVICES=0
savant-b -> NVIDIA_VISIBLE_DEVICES=all, CUDA_VISIBLE_DEVICES=1
```

`savant-b` intentionally mounts both GPUs while restricting CUDA to physical GPU1.
This keeps DeepStream/OpenCV using logical `gpu-id=0` inside the process while
the logical device maps to the second 4090.

Use profile-specific container names and host metrics ports so both modules can
run at the same time. Each Savant instance must have unique ZMQ bind endpoints
inside its own service namespace, and each forwarder must target the matching
Savant service.

Acceptance:

- `docker compose --profile dual-4090-two-source config` succeeds;
- both Savant containers start and report healthy/running;
- both forwarders expose separate health/metrics endpoints;
- `docker ps` shows two Replay, two forwarder, and two Savant containers.

### Phase 4 - Source Runtime Assignment

Update runtime source generation so the two selected RTSP sources start with:

```text
source A ZMQ_ENDPOINT=dealer+connect:tcp://replay-a:5555
source B ZMQ_ENDPOINT=dealer+connect:tcp://replay-b:5555
```

The fixed-source path and dynamic-source path must not drift. If runtime apply is
used, it must write the same shard assignment that `camera_source_controller.py`
uses to start source adapters.

Acceptance:

- `infra/generated/sources.generated.yml` contains shard-specific endpoints for
  the two validation sources;
- source-adapter logs show source A connected to replay-a and source B connected
  to replay-b;
- no validation source sends frames to the legacy `replay-service`.

### Phase 5 - Clip Worker Shard Routing

Set clip-worker shard routing explicitly:

```text
REPLAY_SHARDS_CONFIG_PATH=/app/infra/config/replay-shards.dual_4090_two_source.json
```

If the existing `REPLAY_API_URL` remains present, treat it only as a legacy
fallback. The validation run must prove replay jobs used the shard route from
the event source id.

Acceptance:

- source A record requests create Replay jobs through `http://replay-a:8080`;
- source B record requests create Replay jobs through `http://replay-b:8080`;
- event payload or task metadata preserves `replay_shard_id`,
  `replay_api_url`, `replay_job_sink_url`, and the actual Replay job request;
- replay jobs to the wrong shard are rejected or never created.

### Phase 6 - TensorRT Engine Handling

4090 and T4 are both NVIDIA GPUs, so the runtime architecture and service graph
should stay the same. The hardware-specific exception is TensorRT engine
compatibility.

For this validation:

- `savant-a` may use the existing `/data/video-analytics/models` cache.
- `savant-b` must use an independent model cache, defaulting to
  `/data/video-analytics/models-savant-b`, so GPU1 does not load TensorRT engine
  plan files generated on GPU0. The cache should contain the same ONNX and
  Savant config files, but GPU-specific `.engine` files should be generated by
  `savant-b` on GPU1.
- Prepare the B cache with
  `scripts/runtime/prepare_dual_4090_savant_b_model_cache.sh` before starting
  `savant-b`; this preserves the root ONNX/engine symlink layout expected by
  the generated DeepStream config while still excluding prebuilt `.engine`
  payloads.
- A pass on 4090 validates topology, sharding, routing, and evidence correctness.
- It does not remove the later requirement to generate or validate T4 engines on
  the target T4 machine.

Acceptance:

- both Savant instances load models successfully on 4090;
- `savant-b` startup logs do not show cross-device TensorRT plan reuse warnings;
- no engine path is hard-coded in a way that would prevent later T4 regeneration;
- the spec or run log records the engine source used for the 4090 validation.

### Phase 7 - End-To-End Validation

Run both RTSP sources concurrently until each shard produces at least one
verified evidence bundle.

Required proof:

- `savant-a` has advancing frames/annotations for source A;
- `savant-b` has advancing frames/annotations for source B;
- Redis/Postgres receives events for both source ids;
- clip-worker creates replay jobs on both replay shards;
- `video-file-sink-a` and `video-file-sink-b` each produce real `video.mov`
  output;
- media-worker finalizes at least one evidence bundle per source;
- `/api/v1/evidence/bundles` or 8090 shows both bundles as ready;
- bundle summaries show `production_ready=true` and
  `visual_evidence_status=verified`.

Suggested runtime checks:

```bash
docker compose --env-file infra/env/midterm.env -f infra/docker-compose.midterm.yml --profile dual-4090-two-source config
docker ps --format '{{.Names}}\t{{.Status}}\t{{.Image}}'
curl --noproxy '*' -sS http://127.0.0.1:<forwarder-a-metrics-port>/metrics
curl --noproxy '*' -sS http://127.0.0.1:<forwarder-b-metrics-port>/metrics
curl --noproxy '*' -sS http://127.0.0.1:<savant-a-metrics-port>/metrics
curl --noproxy '*' -sS http://127.0.0.1:<savant-b-metrics-port>/metrics
curl --noproxy '*' -sS 'http://127.0.0.1:8090/api/v1/evidence/bundles?limit=20'
```

## 5. Tests To Add Or Update

Minimum automated coverage:

- compose/static test proving the dual profile has two Replay, two forwarder,
  two Savant, and two sink services;
- shard config test for the two real source ids;
- clip-worker unit test proving source A routes to replay-a and source B routes
  to replay-b;
- runtime apply or source-generation test proving source endpoints are
  shard-specific;
- a smoke script that prints a single PASS token only after both shards produce
  ready/verified evidence.

Suggested PASS token:

```text
PASS_DUAL_4090_TWO_SOURCE_REPLAY_INFERENCE_EVIDENCE
```

## 6. Non-Goals

Do not include these in the next goal unless explicitly requested:

- 60-stream capacity proof;
- 30/30 balancing;
- production T4 TensorRT engine generation;
- full 8090 shard dashboard redesign;
- production-grade shard supervisor failover;
- storage sizing and NVMe endurance validation;
- broad frontend/operator UI changes.

## 7. Rollback

The rollback path must preserve the current single-shard midterm runtime:

```text
replay-service -> analysis-forwarder -> savant-security
```

Rollback requirements:

- keep existing single-shard service names and config files intact;
- stop only the dual profile services when rolling back;
- restore source endpoints to `dealer+connect:tcp://replay-service:5555`;
- unset or empty `REPLAY_SHARDS_CONFIG_PATH` if returning to legacy routing;
- verify the current single-shard evidence flow still produces ready bundles.

## 8. Definition Of Done

This spec is complete only when all are true:

- both 4090 GPUs are actively used by separate Savant containers;
- the two real RTSP sources are assigned to different Replay shards;
- both Replay shards forward analysis frames to their matching forwarder/Savant;
- clip-worker creates replay jobs against the storing shard;
- both sources produce ready/verified evidence bundles in one validation run;
- tests and compose config checks pass;
- the final run records concrete evidence ids for both source ids.
