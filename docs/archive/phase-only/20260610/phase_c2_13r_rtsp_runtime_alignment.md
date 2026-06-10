# Phase C2.13R - RTSP Runtime Source Alignment

## Why C2.13 Was Partial

C2.13 proved that the active C2 runtime input is a real RTSP source:

- input_type: `rtsp`
- RTSP URL: `rtsp://10.37.57.112:8554/live/1080movie`
- runtime source_id: `c2_post_savant_fps_probe`
- runtime camera_id: `c2_post_savant_fps_probe`

It also proved that Savant emits RTSP face observations. The partial result was
caused by runtime alignment gaps:

- the mounted camera config only defined `c1e_rtsp_replay`;
- the active runtime source was `c2_post_savant_fps_probe`;
- therefore `BehaviorRulesPyFunc` could not bind intrusion ROI/rule to the
  active source;
- no face-worker or event-worker container existed in the C2 POC compose stack;
- `security.events` was absent and DB events did not change.

## Alignment

C2.13R adds a deterministic active-source camera entry to:

`modules/savant_security/config/cameras.c1e_replay.yml`

New binding:

- camera_id: `c2_post_savant_fps_probe`
- source_id: `c2_post_savant_fps_probe`
- algorithm_id: `behavior.intrusion`
- rule_id: `c2_13r_rtsp_intrusion_rule`
- ROI: full-frame polygon
- coordinate_space: `pixel`
- points:
  - `[0.0, 0.0]`
  - `[1920.0, 0.0]`
  - `[1920.0, 1080.0]`
  - `[0.0, 1080.0]`
- min_inside_ms: `1`
- cooldown_s: `30`
- min_person_confidence: `0.25`
- min_person_width: `20`
- min_person_height: `40`
- min_visible_keypoints: `0`

The old `c1e_rtsp_replay` entry remains for compatibility; it no longer serves
as the only configured source.

Because Savant loads camera config at startup, a C2.13R smoke may minimally
restart `savant-security` and `source-adapter` when the mounted config changes.
Redis, PostgreSQL, Replay, and evidence-viewer are not restarted.

## Worker Runtime

The current C2 compose does not define face-worker or event-worker services.
C2.13R therefore uses bounded local consumer loops:

- face stream: `security.face_observations`
- face consumer group: `c2_13r_face_worker`
- event stream: `security.events`
- event consumer group: `c2_13r_event_worker`

The bounded face worker:

1. reads new RTSP face observations;
2. inserts them idempotently into PostgreSQL;
3. searches active Reese/Finch gallery embeddings;
4. emits `watchlist_hit` only when similarity is at least `0.65`.

The bounded event worker:

1. reads `security.events`;
2. persists events through the event-worker repository;
3. creates evidence tasks using the existing repository contract.

No fake watchlist or intrusion events are created.

## Watchlist Inputs

Registered C2.12A gallery:

- Reese:
  - `person_id=5`
  - `external_person_id=demo:f4_3:reese`
  - `gallery_embedding_id=4`
- Finch:
  - `person_id=6`
  - `external_person_id=demo:f4_3:finch`
  - `gallery_embedding_id=5`

Threshold:

`0.65`

## Runtime Result

Latest bounded smoke output:

`/data/video-analytics/media/evidence/c2_13r_rtsp_runtime_alignment_20260608T022816/`

Result marker:

`PARTIAL_C2_13R_WATCHLIST_READY_INTRUSION_NO_TRIGGER`

Runtime window:

- requested: `180.0` seconds
- observed: `180.259711` seconds

RTSP/source status:

- input_type: `rtsp`
- source_id: `c2_post_savant_fps_probe`
- camera_id: `c2_post_savant_fps_probe`
- URL redacted: `rtsp://10.37.57.112:8554/live/1080movie`

Config alignment:

- active source bound: `true`
- configured sources: `c1e_rtsp_replay`, `c2_post_savant_fps_probe`
- C2.13R watchlist rule: `c2_13r_rtsp_reese_finch_watchlist_rule`
- C2.13R intrusion rule: `c2_13r_rtsp_intrusion_rule`
- runtime restart during final smoke: `false`

Worker runtime:

- face worker mode: bounded local consumer
- face messages read: `79`
- face observations inserted: `79`
- event worker mode: bounded local consumer
- event messages read: `3`
- event rows inserted: `3`

Watchlist result:

- result: `PASS`
- threshold: `0.65`
- best Reese similarity: `0.4016483040486005`
- best Finch similarity: `0.6879648417675371`
- matched person: Finch
- external_person_id: `demo:f4_3:finch`
- gallery_embedding_id: `5`
- watchlist_hit_count: `3`
- event ids:
  - `41705601-65c2-4032-904d-c0e0112009f4`
  - `788cda04-1d69-4e76-b686-b109b9e9123e`
  - `734f2f63-c93d-4a4e-9fed-d3a4702bbb97`

Intrusion result:

- result: `PARTIAL_C2_13R_INTRUSION_NO_POSE_PERSON`
- ROI bound to runtime source: `true`
- person/pose observation count: `0`
- intrusion_event_count: `0`

Safety:

- payload_has_embedding: `false`
- payload_has_image_bytes: `false`
- unsafe payload scan: passed
- event-style Replay claimed: `false`

## Output

Output directory format:

`/data/video-analytics/media/evidence/c2_13r_rtsp_runtime_alignment_YYYYMMDDTHHMMSS/`

Files:

- `runtime_config_alignment.json`
- `rtsp_source_status.json`
- `roi_rule_config.json`
- `worker_runtime_status.json`
- `watchlist_metrics.json`
- `intrusion_metrics.json`
- `event_worker_metrics.json`
- `api_query_results.json`
- `unsafe_payload_scan.json`
- `decision_summary.json`
- `operator_rtsp_watchlist_intrusion_alignment_report.html`

If events are produced, the tool also writes:

- `watchlist_event.json`
- `intrusion_event.json`
- `persisted_event_rows.json`

## Limitations

- This is a bounded runtime smoke, not a long-running soak.
- This is not broad recognition accuracy testing.
- Event-style Replay is still not passed.
- Visual evidence is optional in C2.13R.
- If Reese/Finch do not appear in the bounded window, watchlist remains partial.
- If no person enters the configured ROI after runtime reload, intrusion remains
  partial.
