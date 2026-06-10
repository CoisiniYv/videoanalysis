# Midterm Operator Algorithm Controls Runtime Status

Date: 2026-06-10

Status: stage snapshot. This document records the actual implementation state of
the 8090 operator camera parameters after the current audit. It is intentionally
limited to documentation; no runtime behavior is changed by this note.

## Scope

The 8090 camera page exposes three groups of controls that can be confused with
fully enabled runtime switches:

- camera source parameters: name, RTSP URL, site, location, enabled state;
- algorithm controls: per-camera enable/disable switches, zone or line binding;
- evidence policy: `pre_seconds`, `post_seconds`, `snapshot_required`,
  `clip_required`.

The current implementation has three different levels of support:

1. saved in the operator/API database;
2. exported into runtime configuration and loaded by Savant;
3. actively used by detection, event persistence, record requests, and evidence
   generation.

Only the third level should be treated as end-to-end implemented.

## Runtime Entry

The active customer entrypoint is:

```text
http://0.0.0.0:8090/
```

The 8090 service is `services/evidence-viewer`. It serves the operator portal
and proxies same-origin `/api/v1/*` calls to the internal API service:

```text
browser -> evidence-viewer:8090 -> api:8000
```

`api:8000` remains internal to the compose network.

## Implemented End To End

### Camera Source Configuration

The basic camera fields are implemented end to end:

- `id`
- `source_id`
- `name`
- `rtsp_url`
- `site_id`
- `location`
- `enabled`
- `input_type=rtsp`
- `rtsp_transport`
- `fps_policy`
- `alert_policy`

The 8090 page saves these fields through `/api/v1/cameras`. Runtime apply writes
the generated camera config, writes the generated source-adapter config,
restarts Savant, and starts dynamic RTSP adapters for enabled non-primary RTSP
sources when `CAMERA_RUNTIME_APPLY_ENABLED=true`. The midterm compose enables
runtime apply by default.

### Zones And Lines

Detection zones are persisted and exported:

- polygon zones are used by supported behavior rules;
- line and direction-line zones are accepted for line-bound rules;
- API validation checks that referenced zones belong to the camera.

### Intrusion Rule And Evidence Window

`behavior.intrusion` is the fully implemented behavior-rule path.

For intrusion, the operator values flow through the full chain:

```text
8090 algorithm card
  -> /api/v1/cameras/{camera_id}/algorithm-rules
  -> camera_rules.evidence_policy
  -> cameras.midterm.yml
  -> Savant behavior rule event.evidence_policy
  -> event-worker record_request
  -> clip-worker Replay request
  -> media-worker evidence bundle
```

The configured `pre_seconds` and `post_seconds` therefore affect intrusion
record requests and the target evidence window, subject to the separate Replay
and media-worker duration guards.

## Partially Implemented

### Behavior Rules With Detection But Limited Evidence Routing

These behavior algorithms are present in the API registry, exported into runtime
config, mapped to Savant runtime rule types, and have concrete rule modules
registered under `modules/savant_security/custom/rules/`:

- `behavior.crowd_gathering`
- `behavior.fall`
- `behavior.chasing`

They can produce behavior events when configured correctly. However, the current
midterm recording/evidence policy defaults are still narrow:

- event-worker creates pending evidence tasks for `intrusion`,
  `watchlist_hit`, `live_search_hit`, and `face_intelligence` events;
- other behavior events start as `not_implemented`;
- compose defaults `RECORDING_EVENT_TYPES` to `watchlist_hit,intrusion`.

So these algorithms are not equivalent to intrusion for production evidence.
They are event-capable, but not fully evidence-enabled in the default midterm
runtime.

### Behavior Rules Declared But Not Registered

These algorithms can be saved by the operator/API and exported in config, but
there is no concrete registered rule module in `custom/rules/` at this snapshot:

- `behavior.loitering`
- `behavior.running`
- `behavior.wall_climb_suspicious`

Savant's rule registry tolerates unknown rule types and skips them with a log
message. Operators should not treat these switches as active detection controls
until the corresponding rule modules are implemented and registered.

## Configuration Only In The Current Runtime

### Face Observation

`face.observation` can be saved and exported as a per-camera algorithm rule, but
the current Savant face observation exporter does not use the rule's `enabled`
state as a runtime gate. The face observation path is controlled by module
configuration and environment variables, and the camera config is currently used
mainly to resolve `source_id` to the business `camera_id`.

### Watchlist

`face.watchlist` can be saved and exported, but the current watchlist matching
runtime is controlled by face-worker environment variables such as:

- `WATCHLIST_MATCH_ENABLED`
- `WATCHLIST_THRESHOLD`
- `WATCHLIST_TARGET_EXTERNAL_PERSON_IDS`
- `WATCHLIST_TARGET_NAMES`

The watchlist event builder uses its own default evidence policy
(`pre_seconds=5`, `post_seconds=10`) and does not read the per-camera
`face.watchlist` rule from the 8090 page.

### Live Search

`face.live_search` is currently contract-only/deferred. The command-line face
match emitter only allows `watchlist_hit`, and the service reports
`live_search_hit` as deferred contract state. The 8090 switch should not be
treated as an implemented live-search runtime.

## Default Window Mismatch

There is an important default mismatch:

- 8090 rule templates default to `pre_seconds=5`, `post_seconds=10`;
- event-worker, clip-worker, media-worker, and midterm env defaults still use
  `DEFAULT_PRE_SECONDS=5`, `DEFAULT_POST_SECONDS=5`;
- API camera export has a legacy fallback default of `post_seconds=5`, but an
  explicit `evidence_policy` from the algorithm-rule API overrides it.

For supported behavior rules such as intrusion, the explicit operator rule
policy is the important value and can override the older 5-second post default.
For face/watchlist events, the 8090 per-camera policy is not currently consumed.

## Stage Boundary

This snapshot should be treated as:

```text
Operator configuration surface: implemented
Behavior intrusion runtime/evidence path: implemented
Some behavior detection rules: implemented without default evidence parity
Face algorithm switches: saved/exported, not runtime gates
Live search: deferred
```

This stage does not claim:

- all visible algorithm switches are active runtime detectors;
- all visible algorithm switches generate Replay evidence;
- per-camera face observation/watchlist/live-search rules control the face
  pipeline;
- runtime apply hot-reloads Savant without restart;
- UI defaults and all worker fallback defaults are fully aligned.

## Follow-Up Plan

Before exposing these controls as production-ready customer switches, choose one
of these directions for each unsupported or partial algorithm:

1. Hide or mark unsupported controls in the operator UI.
2. Add an API support matrix that distinguishes `configurable`,
   `runtime_detecting`, and `evidence_enabled`.
3. Implement and register missing behavior rule modules for loitering, running,
   and wall climb.
4. Extend event-worker recording policy if crowd, fall, or chasing should
   produce Replay evidence by default.
5. Wire face-worker/Savant face components to per-camera algorithm rules if
   face observation, watchlist, and live search must become true operator
   switches.
6. Align default post-event seconds across UI, API fallback, and worker env
   defaults once the desired default window is finalized.

## Source Files Audited

The current status is based on these active files:

- `infra/docker-compose.midterm.yml`
- `services/evidence-viewer/app/main.py`
- `services/evidence-viewer/app/static/index.html`
- `services/evidence-viewer/app/static/operator.js`
- `services/api/app/algorithm_ids.py`
- `services/api/app/algorithm_registry.py`
- `services/api/app/routers/algorithms.py`
- `services/api/app/routers/cameras.py`
- `services/api/app/schemas/algorithms.py`
- `services/api/app/schemas/cameras.py`
- `services/api/app/services/runtime_apply.py`
- `modules/savant_security/custom/services/algorithm_activation.py`
- `modules/savant_security/custom/services/rule_runtime.py`
- `modules/savant_security/custom/rules/`
- `modules/savant_security/custom/pyfuncs/behavior_rules.py`
- `modules/savant_security/custom/pyfuncs/face_observation_exporter.py`
- `modules/savant_security/custom/pyfuncs/face_reid_gate.py`
- `services/event-worker/app/worker.py`
- `services/event-worker/app/record_request.py`
- `services/event-worker/app/repository.py`
- `services/clip-worker/app/worker.py`
- `services/face-worker/app/config.py`
- `services/face-worker/app/face_match_event_service.py`
- `services/face-worker/emit_face_match_events.py`
