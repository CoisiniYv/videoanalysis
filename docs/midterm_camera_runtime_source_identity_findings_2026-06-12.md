# Midterm Camera Runtime and Source Identity Findings

Date: 2026-06-12

This note freezes the read-only diagnosis for the current camera output and
source identity confusion. No runtime apply, container restart, or code change
was performed during this diagnosis.

## Summary

The `lab` camera is present in the database and generated configuration, but it
is not present in the running ingest topology. The running midterm stack has
only the fixed compose source adapter:

```text
video-analytics-midterm-source-adapter
SOURCE_ID=primary_rtsp
RTSP_URI=rtsp://192.168.1.105:8554/live/1080movie
```

There is no dynamic source adapter container for:

```text
video-analytics-source-source_00000000-0000-4000-8000-781078565686
```

Therefore `lab` is saved as a camera configuration, but its RTSP stream is not
currently entering Replay or Savant. Recent events are still produced by
`primary_rtsp`, not by `lab`.

The long displayed value such as
`source_00000000-0000-4000-8000-781078565686` is the machine `source_id`, not
the human camera name. The frontend creates this default `source_id` for new
cameras, and the evidence UI currently displays `source_id` when it should show
the camera display name and keep IDs as technical details.

## Runtime Evidence

Database camera row observed on 2026-06-12:

```text
id=00000000-0000-4000-8000-781078565686
source_id=source_00000000-0000-4000-8000-781078565686
name=lab
enabled=true
rtsp_url=rtsp://10.37.57.157:8554/camera
```

Generated runtime config also contains the camera:

```text
modules/savant_security/config/cameras.midterm.yml
infra/generated/sources.generated.yml
```

The generated source entry is enabled and points at the lab RTSP URI:

```text
source_id: source_00000000-0000-4000-8000-781078565686
uri: rtsp://10.37.57.157:8554/camera
enabled: true
zmq_endpoint: dealer+connect:tcp://replay-service:5555
```

The running supervisor endpoint returned only the fixed source adapter:

```json
{
  "source_adapters": ["video-analytics-midterm-source-adapter"],
  "running_source_adapters": ["video-analytics-midterm-source-adapter"],
  "savant_module_status": "running",
  "annotation_stream": "security.frame_annotations",
  "annotation_age_s": 0
}
```

There were no containers whose names matched `video-analytics-source-*`.

Recent event source distribution at the time of inspection:

```text
primary_rtsp | 00000000-0000-4000-8000-000000000100 | intrusion | 53
```

No recent event was observed for
`source_00000000-0000-4000-8000-781078565686`.

## Component Boundaries

`camera.name` is a human display label. In this case it is `lab`.

`source_id` is the stable machine stream identity. Savant, Replay, Redis
messages, PostgreSQL events, evidence bundles, and file sink paths all rely on
it to separate streams.

`source-adapter` is the RTSP ingest process. With the current GStreamer RTSP
adapter deployment, one active RTSP source normally maps to one source-adapter
process or container. That does not mean one Savant module or one Replay
service per camera.

`savant-security` is the inference module. The official Savant model supports
multiple streams through one module and distinguishes streams by `source_id`.

`replay-service` is a shared recording/restreaming service in this topology.
It is not a per-camera service. Source adapters feed Replay, Replay forwards
live frames to Savant, and clip creation asks Replay to write selected windows
to `video-file-sink`.

`media-worker` finalizes evidence after clips are written. It is not part of
RTSP ingest or inference.

## Official Savant Model

The official Savant pattern is source adapters plus sink adapters around a
shared module. Multiple streams can be processed by one module, with stream
identity carried by `source_id`. Stateful custom code must key state by
`source_id`.

The official isolation primitive is not one database table or one pipeline per
camera. It is a unique source identity carried through frames, metadata, sinks,
and downstream application records.

Savant Replay is a separate service for buffering and replay jobs. It should
not be multiplied once per camera unless there is a deliberate scaling or
failure-domain reason. The current midterm design should prefer:

```text
camera A source-adapter \
camera B source-adapter  -> shared replay-service -> shared savant-security
camera C source-adapter /
```

or a validated multi-stream adapter equivalent, not:

```text
camera A -> replay A -> savant A
camera B -> replay B -> savant B
```

## Current Implementation Gap

The midterm compose file still contains one fixed source adapter for
`primary_rtsp`. Additional cameras are expected to become dynamic source
adapter containers using the `video-analytics-source-` prefix.

The runtime apply implementation can create these dynamic source adapters, but
the current apply path is heavy: it writes config, recreates sink state,
restarts Replay/Savant/workers, and then starts source adapters. That can
restore a missing source, but it is disruptive and should not be the long-term
mechanism for every camera add, enable, disable, or RTSP URL edit.

The present runtime state is a convergence gap:

```text
desired state:
  primary_rtsp enabled
  source_00000000-0000-4000-8000-781078565686 enabled

actual state:
  only primary_rtsp source adapter is running
```

## Naming/UI Gap

The operator page creates a new camera with:

```javascript
source_id: `source_${cameraId}`
```

This is acceptable as a stable machine default, but it should not be the main
label shown to operators.

Evidence list rendering currently prefers:

```javascript
bundle.source_id || bundle.camera_id || "unknown camera"
```

That explains why the UI showed a long `source_...` value even though the
registered camera name was `lab`.

The fix should keep `source_id` visible in technical details, but primary UI
labels should resolve and display `camera.name`, for example:

```text
lab
source_00000000-0000-4000-8000-781078565686
00000000-0000-4000-8000-781078565686
```

## Repair Candidates

1. Add a source-only runtime convergence path.

   Camera add, enable, disable, and RTSP URL edits should start, stop, or
   recreate only the affected source adapter when the module/replay topology
   does not change. Full runtime apply should remain available for changes that
   require Savant, Replay, sink, or worker restart.

2. Make supervisor report desired versus actual source state.

   The supervisor should report enabled cameras from config/DB, expected
   source adapter container names, running adapter names, and missing or stale
   adapters. The UI can then show `lab: configured but not running` instead of
   silently showing only old `primary_rtsp` output.

3. Fix evidence display naming.

   Prefer a backend join that includes `camera_name` in evidence bundle list
   and detail responses. If a backend change is not immediately available,
   frontend can cache `/api/v1/cameras` and map `camera_id` or `source_id` to
   `camera.name`.

4. Keep `source_id` stable and unique.

   Do not change existing `source_id` values casually after events or evidence
   exist. Treat `source_id` as part of the stream identity contract.

5. Add targeted tests.

   Useful coverage:

   - enabled non-primary camera produces an expected dynamic source container;
   - disabled camera removes or stops its dynamic source container;
   - evidence APIs expose `camera_name`;
   - evidence UI prefers camera display name over `source_id`;
   - supervisor flags configured-but-not-running sources.

## Operational Note

The immediate recovery operation is to run the existing runtime apply endpoint,
but that restarts more of the chain than necessary:

```text
POST /api/v1/cameras/runtime/apply
```

That endpoint was not called during this diagnosis. Use it only when the
runtime disruption is acceptable, or after the source-only convergence path is
implemented.

## Related Findings

- `docs/midterm_media_worker_snapshot_performance_findings_2026-06-12.md`
- `docs/runtime_stability_fix/midterm_worker_savant_batching_findings.md`

## Code Pointers

- `infra/docker-compose.midterm.yml`
  - fixed `source-adapter` has `SOURCE_ID=primary_rtsp`
- `modules/savant_security/config/cameras.midterm.yml`
  - desired camera config includes `lab`
- `infra/generated/sources.generated.yml`
  - desired source config includes `lab`
- `services/api/app/services/runtime_apply.py`
  - dynamic source container lifecycle and runtime apply sequence
- `services/evidence-viewer/app/static/operator.js`
  - new camera default `source_id`
- `services/evidence-viewer/app/static/evidence.js`
  - evidence list currently displays `source_id`

## Official References Checked

- `https://savant-ai.io/docs/latest/savant_101/00_streaming_model.html`
- `https://savant-ai.io/docs/latest/savant_101/10_adapters.html`
- `https://savant-ai.io/docs/latest/advanced_topics/0_batching.html`
- `https://savant-ai.io/docs/latest/advanced_topics/17_restreaming.html`
