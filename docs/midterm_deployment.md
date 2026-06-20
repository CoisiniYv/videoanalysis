# Midterm Deployment

Date: 2026-06-11

This is the active project-machine deployment entrypoint.

## Files

| Purpose | File |
|---|---|
| Compose | `infra/docker-compose.midterm.yml` |
| Env | `infra/env/midterm.env` |
| Replay config | `modules/savant_replay/config.midterm.json` |
| Analysis-forwarder | `services/analysis-forwarder/` |
| Camera config | `modules/savant_security/config/cameras.midterm.yml` |
| Savant module | `modules/savant_security/module.yml` |
| Savant v0.6.0 patch overlay | `modules/savant_security/savant_patches/` |
| Savant supervisor | `services/api/app/services/savant_supervisor.py` |

Do not deploy from archived historical compose files.

## Start

```bash
docker compose -f infra/docker-compose.midterm.yml config
docker compose -f infra/docker-compose.midterm.yml up -d --build
```

Lightweight pre-deploy check:

```bash
bash scripts/smoke/current/check_midterm_deployment.sh
```

Read-only deployment doctor:

```bash
bash scripts/runtime/doctor_midterm.sh
```

The stack uses these default host ports:

- Redis: `6396`
- Replay API: `8098`
- Analysis-forwarder metrics: `18081`
- Operator portal / evidence viewer: `8090`
- Internal API service: compose network port `8000`; not published to host and
  reached through the 8090 portal proxy.

The internal API service is built with `services/api/Dockerfile.face-runtime`.
It inherits from `video-analytics-midterm-face-worker:latest`, so face
registration can reuse the existing ONNX Runtime/OpenCV/Numpy image layer
instead of reinstalling ORT during API builds. On a fresh deployment machine,
build or provide `video-analytics-midterm-face-worker:latest` before building
the internal API image.

The 8090 operator portal, internal API proxy, face-registration runtime, and
evidence identity semantics are documented in
`docs/midterm_operator_portal_runtime_design.md`.
The current implementation boundary for 8090 algorithm switches and evidence
window fields is documented in
`docs/midterm_operator_algorithm_controls_runtime_status.md`.
The current `/data/video-analytics` directory inventory and cleanup record is
documented in `docs/midterm_data_directory_inventory.md`.
The 2026-06-10 Replay `routing_id` mismatch incident and recovery procedure are
documented in `docs/midterm_replay_routing_id_recovery.md`.

## Savant Source-Reset Hardening

The midterm Savant image is pinned to
`ghcr.io/insight-platform/savant-deepstream:0.6.0-7.1`. That framework version
can remove a source registry entry after non-monotonous PTS resets while stale
buffers for the same source remain queued in muxer or nvinfer stages. Without a
guard, those buffers can raise `KeyError` inside Savant framework code and move
the module to STOPPED while the Docker container remains Up.

To prevent that failure mode, `savant-security` runs
`modules/savant_security/savant_patches/apply_patches.py` before starting
`python -m savant.entrypoint`. The patch is md5-pinned to the current v0.6.0
framework files and fails loud by default on mismatch. It only changes stale
source-buffer handling from fatal `KeyError` to warning plus frame skip or
late-EOS ignore.

The API service behind the 8090 management plane owns the recovery supervisor.
It reads `/opt/savant/status.txt` through the Docker Engine API and checks
`security.frame_annotations` through Redis; if Savant is STOPPING/STOPPED or
annotations stall while sources are running, it restarts Savant, waits for
module `running`, then restarts both the compose primary adapter and dynamic
`video-analytics-source-*` adapters. It does not restart Replay by default,
unless `SAVANT_SUPERVISOR_RESTART_REPLAY=true` is set.

The reviewed `savant-crash-fix-20260611.zip` proposed a standalone watchdog
container based on `docker:27-cli`. That image is not required in the current
deployment. The standalone service has been removed; the API process reuses the
existing Docker socket mount and Docker Engine client already needed for camera
runtime apply. 8090 remains the operator-facing surface through the evidence
viewer proxy.

Supervisor routes exposed through 8090:

- `GET /api/v1/cameras/runtime/supervisor`
- `POST /api/v1/cameras/runtime/supervisor/recover`

## Applying Camera Runtime Changes

The 8090 camera page writes camera, zone, and algorithm-rule records to the
internal API database. The page exposes per-rule enable/disable controls and the
rule-level evidence window (`pre_seconds` / `post_seconds`). Event recording is
not restricted to `primary_rtsp`; any configured source that reaches Savant and
emits a clip-required event can publish a record request.

Current support is intentionally uneven across visible algorithm switches. Treat
`behavior.intrusion` as the end-to-end implemented behavior evidence path. Some
behavior switches are config/export only or event-capable without default Replay
evidence parity, and face algorithm switches are not yet true per-camera runtime
gates. Keep `docs/midterm_operator_algorithm_controls_runtime_status.md` in sync
when changing this boundary.

The 8090 page's runtime apply action exports `cameras.midterm.yml`, stops source
adapters and workers, recreates the Replay sink epoch, restarts Replay and
Savant, restores workers, then starts enabled source adapters last. The default midterm
recording window is `pre_seconds=5` and `post_seconds=5`; changing those values
in the operator page writes them to the rule evidence policy before applying the
runtime.

The legacy primary stream is represented in the database as a UUID camera row
with `source_id: primary_rtsp`. Keep `primary_rtsp` as the source id because it
is what the compose-managed adapter and Savant source mapping use; do not use it
as the database primary key. The operator smoke camera is test data and is
created disabled by default so normal runtime exports exclude its fake RTSP URL.

For a new RTSP camera to participate in inference, apply the runtime in this
order:

1. Add or update the camera from the 8090 operator portal.
2. Add at least one zone and one enabled algorithm rule when event/evidence
   output is expected. For alerting rules, set the rule evidence window on the
   8090 page; those values are carried into the record request. A camera with no
   enabled rules can still feed lower-level detections, but it will not produce
   rule events.
3. Use the 8090 page's `应用运行时` button, or save the camera/zone/rule and let
   the page auto-apply runtime changes. The runtime apply endpoint remains
   behind the 8090 evidence-viewer proxy; the API service is still only exposed
   inside the compose network on port 8000.

The runtime apply operation writes both generated config files, stops all
source-adapter containers first, recreates dynamic RTSP source-adapter
containers for non-primary sources, restarts Replay, analysis-forwarder, and
Savant, restores workers, then starts enabled sources last. This ordering clears
Replay's ZeroMQ routing identity cache and prevents the source adapters from
continuing to send frames through stale connections. When the operation
recreates `video-file-sink`, it
must also preserve the Docker network alias `video-file-sink`, because
clip-worker Replay jobs use
`dealer+connect:tcp://video-file-sink:6666` as their sink URL. A manually
recreated sink container without that alias will keep listening on port 6666
but Replay will not resolve the peer, so no raw clip or evidence bundle will be
written. The official `video-file-sink` native metadata does not preserve
Replay labels, so media-worker treats `sink_metadata_runtime_epoch_id` as
optional evidence: it fails only when the field is present and mismatched, while
event payload, record request, Replay labels, sink path, and current epoch remain
required. The config export part can be run manually with:

```bash
python scripts/config/export_runtime_configs.py \
  --api-base-url http://localhost:8090 \
  --module-config-output modules/savant_security/config/cameras.midterm.yml \
  --sources-output infra/generated/sources.generated.yml \
  --zmq-endpoint dealer+connect:tcp://replay-service:5555
```

The explicit `--zmq-endpoint dealer+connect:tcp://replay-service:5555` keeps
the midterm replay-first ingest path:

```text
RTSP adapter -> replay-service -> analysis-forwarder -> savant-security
```

The adapter still sends full-rate frames to Replay. Replay stores the full-rate
stream and sends its analysis `out_stream` to `analysis-forwarder`. The
forwarder samples/drops only the analysis branch, then writes accepted frames to
Savant. Evidence Replay jobs still read from Replay and write to
`video-file-sink`; they do not use the sampled forwarder output.

The Savant service must not set a single-source `SOURCE_ID` filter. The
compose-managed primary adapter still uses `SOURCE_ID=primary_rtsp`, but Savant
itself accepts all replay-service sources and resolves each one through
`cameras.midterm.yml`. `MAX_PARALLEL_STREAMS` must be at least the number of
simultaneous RTSP sources you expect to infer with headroom; the midterm default
is configurable as `${MAX_PARALLEL_STREAMS:-4}`.
Dynamic RTSP adapters started by `scripts/runtime/camera_source_controller.py`
use `EOS_ON_START=false` and do not set `USE_ABSOLUTE_TIMESTAMPS`. In the
midterm replay-first path, an EOS-on-start closes the new source before frames
arrive, and absolute PTS can make replay defer forwarding live RTSP frames to
Savant.

4. If you are applying manually instead of using 8090 runtime apply, do not only
   restart Savant. Use the 8090 controlled restart endpoint, which keeps the
   8090 management plane online while restarting the controlled runtime:

```bash
curl --noproxy '*' -X POST http://0.0.0.0:8090/api/v1/cameras/runtime/restart
```

This controlled restart is required in the current implementation because
Replay holds source-adapter ROUTER/DEALER connection identity, and the behavior
rule, face, and frame-annotation runtime load camera mapping at process startup.
Restarting is not a replacement for exporting the config; if
`cameras.midterm.yml` does not contain the new `source_id`, Savant will still
not route that source as a configured camera.

5. Start the RTSP source adapter for the new source:

```bash
python scripts/runtime/camera_source_controller.py start \
  --sources infra/generated/sources.generated.yml \
  --source-id <source_id> \
  --network video-analytics-midterm_default
```

6. Verify the adapter and Savant logs:

```bash
python scripts/runtime/camera_source_controller.py status \
  --sources infra/generated/sources.generated.yml \
  --source-id <source_id>

docker logs --tail 200 video-analytics-midterm-savant | rg '<source_id>|unknown_source|behavior_rules_init'
```

Expected signs:

- `behavior_rules_init` lists the new source in `sources=[...]`.
- Recent Savant logs include `source_id=<source_id>`.
- There is no `savant_security_behavior_rules_unknown_source` for the new
  source.

If `GET /api/v1/cameras/config/export` fails, fix that API/export error before
starting the adapter. Starting an adapter without a matching exported runtime
config can deliver frames, but the configured camera/rule pipeline will not be
correct.

Workers use the existing host PostgreSQL by default:

```text
postgresql://video:video@host.docker.internal:5432/video_analytics
```

Use the `local-postgres` compose profile only when you explicitly want the
stack-local PostgreSQL on host port `5439`.

## Evidence Policy

The evidence path is replay-first:

```text
RTSP -> Replay -> analysis-forwarder -> Savant -> Redis/PostgreSQL
  -> clip-worker Replay job -> video-file-sink -> media-worker sidecar
```

Replay is the evidence source of truth. `analysis-forwarder` is part of the
analysis path and is allowed to drop sampled analysis frames under pressure; it
must not be used as the source for `raw_clip.mov`.

Evidence bundles contain:

- `raw_clip.mov`
- `sink_metadata.json`
- `annotations.frame_cache.identity.jsonl`
- `summary.frame_cache.identity.json`
- `metadata.json`

The media-worker writes `project_version=midterm` and `schema_version=2.0-midterm`
for this deployment. Legacy metadata fields are disabled in the midterm compose.

New evidence metadata must carry the alarm machine time in `metadata.json`:

```json
{
  "event": {
    "created_at": "<events.created_at>",
    "alarm_machine_time": "<events.created_at>",
    "alarm_machine_time_source": "events.created_at"
  }
}
```

The 8090 evidence viewer also returns `alarm_machine_time` and
`alarm_machine_time_source` from `/api/bundles` and `/api/bundles/{event_id}`.
For old bundles without `created_at`, it may derive a display value only from
epoch-millisecond-looking fields. Frame-relative or video-relative timestamps
are not accepted as machine time.

## Runtime Calibration

Current defaults are set in `infra/env/midterm.env`:

- Analysis-forwarder FPS: `8/1`
- Savant project ingress FPS gate: enabled, `8/1`
- Savant nvstreammux `MAX_FPS_CONTROL`: disabled after Phase 0A
- Pose infer interval: `1`
- Pose detector/selector thresholds: `0.50`
- Pose keypoint threshold: `0.35`
- Pose minimum box: `60x100`
- Face detector threshold: `0.50`
- Watchlist similarity threshold: `0.60`

These values are chosen to reduce excessive pose boxes, slow the inference path
to a reasonable rate, and keep low-quality face detections out of comparison.

## Archive Locations

Historical files are preserved here:

- `infra/archive/phase-only/20260609/`
- `modules/savant_replay/archive/phase-only/20260609/`
- `modules/savant_security/config/archive/phase-only/20260609/`
- `docs/archive/phase-only/20260610/`
- `harness/tests/archive/phase-only/20260610/`
- `scripts/*/archive/phase-only/20260610/`
- `services/archive/phase-only/20260610/`

They are not deployment entrypoints.
