# C2 Post-Savant Watchlist Evidence Rebaseline

Date: 2026-06-07

## Executive Summary

C2 now has a demonstrable watchlist evidence path:

```text
post-Savant stable sink
-> time-domain evidence crop
-> production sidecar
-> identity patch
-> watchlist_hit event semantics
-> face-worker Redis consumer proof
-> event-worker persistence
-> API evidence detail
-> live API / evidence-viewer runtime verification
```

The path is demo-ready for a controlled watchlist evidence walkthrough. It
shows an operator-visible `watchlist_hit` with a `known_face`, person identity,
match score, threshold, `source_observation_id`, evidence bundle files, video
integrity status, and the current workaround limitations.

This is not a Replay repair phase and not a recognition-accuracy phase.

## Current Final Status

Current result marker:

`PASS_C2_10_RUNTIME_API_VIEWER_READY`

C2.10 verified:

- live API HTTP evidence detail using a temporary local Uvicorn API process;
- evidence-viewer runtime at `http://127.0.0.1:8090`;
- bundle manifest HTTP 200;
- sidecar annotations HTTP 200;
- static operator report generation;
- unsafe payload scan with no embedding vectors and no image/base64/crop bytes.

Important caveats:

- Event-style Replay evidence flow is not passed.
- Current evidence capture mode is
  `stable_post_savant_sink_time_crop`.
- `event_style_replay_job_passed=false` remains true throughout the evidence
  summaries.
- Broad recognition accuracy is not proven.
- Long-running worker soak is not proven.
- C2.10 live API HTTP used temporary local Uvicorn, not a live production API
  container.

## Milestone Table

| Phase | Result marker | Commit hash | What it proved | Key output path | Limitation |
| --- | --- | --- | --- | --- | --- |
| C2.0 | `PASS_C2_POST_SAVANT_REPLAY_METADATA_RETAINED` | `efbf6493d3a7bf6d7f0ae3a744c3e6bff4c407a3` | Replay output from a post-Savant stream retained object metadata: person bbox, tracks, pose keypoints, face bbox, and landmarks. | `/data/video-analytics/media/c2-post-savant-replay-poc/c2-poc-20260607T105448%/unknown%/` | No `source_observation_id`, no identity binding, no watchlist semantics. |
| C2.1 | post-Savant sidecar builder verified | `ab2a9bac2357e59ad8ae2dfdc174d1ae5491329b` | Post-Savant `metadata.json` can be converted into production sidecar files. | `annotations.frame_cache.identity.jsonl` from post-Savant sink metadata | Faces were observation-only / unknown. |
| C2.1B | viewer runtime verification added | `2431606dae481f1fd1de2c7ff4a1384eb01b027a` | Evidence-viewer could load the C2 production sidecar wrapper bundle. | `/data/video-analytics/media/evidence/c2_1_viewer_runtime_20260607T121530` | Still no known face or watchlist proof. |
| C2.1C | face observations rendered from production sidecar | `99a3c14665a51f33415baa338474a17706683e2e` | Viewer overlay rendered face observations, person bbox, pose keypoints, and landmarks from the sidecar. | C2.1C viewer samples | No gallery identity binding. |
| C2.2 | `FAIL_C2_2_VISUAL_TIMELINE_MISALIGNED` | not committed as final code | Naive standard bundle trimming was not production-safe because tail frames drifted. | `/data/video-analytics/media/evidence/c2_2_20260607T132851` | Failed timeline alignment; isolated WIP only. |
| C2.2A | `PASS_C2_2A_FPS_GATING_STABILIZES_TIMELINE` | documented in `docs/c2_rebaseline_2026_06_07.md` | A fresh FPS-gated post-Savant probe produced stable 10 second visual alignment. | `/data/video-analytics/media/c2-post-savant-replay-fps-probe/c2-fps-probe-20260607T140420%/unknown%` | Not event anchored as event-5s/event+5s. |
| C2.2R / C2.3A | evidence bundle packaging flow | `031324c5fee32ee2521bbbd212e290ef3001fe23`, `d840c432edf03536aa2454b04fd9a527b73a0d46` | FPS-gated post-Savant metadata was packaged as an evidence bundle and integrated into the media-worker flow. | `/data/video-analytics/media/evidence/c2_3a_20260607T161416` | Watchlist trigger identity was not verified yet. |
| C2.3Q | `PASS_C2_3Q_WITH_POSE_CLIPPING_WARNINGS` | `0b75f3ecd3be8461dc3722916f83ab768363cd76` | Visual/structural evidence audit passed with manually accepted pose/keypoint clipping warnings. | `/data/video-analytics/media/evidence_audit/c2_3q_20260607T164710` | Known pose/keypoint warning class remains manually accepted; no identity proof. |
| C2.3B-R2 | `PARTIAL_C2_3B_R2_STABLE_SINK_WORKAROUND_READY` | `25f74ffc3ba61e2f20ae3515b889cc447b2fd48f` | Time-domain video integrity gate was added; event-style Replay failed closed, but stable sink crop passed as a workaround. | `/data/video-analytics/media/evidence/c2_3b_r2_20260607T201306_stable_sink_crop` | Not `PASS_C2_3B_EVENT_CLIP_POST_SAVANT_REPLAY_MAPPING_READY`. |
| C2.3B-R2H | `PARTIAL_C2_3B_R2_STABLE_SINK_WORKAROUND_READY` boundary documented | `156d44af20983ec2329e5c9712368e664905ddf1` | The stable sink workaround boundary and Replay backlog were explicitly recorded. | `docs/phase_c2_3b_r2_stable_sink_workaround_rebaseline.md` | Event-style Replay remains backlog. |
| C2.4 | `PASS_C2_4_IDENTITY_BINDING_EVIDENCE_PATCH_READY` | `54b14e498063badb9ebf632e5d7bccc35ef78b8f` | A C2 sidecar face was patched to `known_face` using real match/gallery/person lineage without changing geometry. | `/data/video-analytics/media/evidence/c2_4_identity_binding_20260607T211846` | Deterministic test person; plumbing proof, not broad accuracy. |
| C2.5 | `PASS_C2_5_WATCHLIST_EVENT_EVIDENCE_SEMANTICS_READY` | `fddd7006241c5c9ff64e16a60c2357385fbd60f4` | The known face/gallery match was represented as a proper `watchlist_hit` evidence event. | `/data/video-analytics/media/evidence/c2_5_watchlist_hit_20260607T214030` | Offline watchlist semantics; not face-worker runtime generation. |
| C2.6 | `PARTIAL_C2_6_FACE_WORKER_HARNESS_ONLY` | `0aa7d2d782fce1cc7f8f02b47a94016593229a9c` | The face-worker pgvector/gallery matching path generated watchlist evidence semantics through a harness. | `/data/video-analytics/media/evidence/c2_6_live_watchlist_20260607T215648` | Did not run Redis consumer loop or publish to `security.events`. |
| C2.6R | `PASS_C2_6R_REDIS_CONSUMER_WATCHLIST_EVENT_READY` | `2ee39a9a1e9f1329da6eca4c4a9978290963cdf7` | Isolated Redis stream one-message face-worker consumer produced a real `watchlist_hit` into an isolated event stream. | `/data/video-analytics/media/evidence/c2_6r_redis_watchlist_20260607T221035` | One-message proof, not long-running face-worker soak. |
| C2.7 | `PASS_C2_7_EVENT_WORKER_PERSISTENCE_API_QUERY_READY` | `26d18135532efb1b962de01a334c962d0ce45515` | Event-worker consumed the C2.6R event, persisted it to PostgreSQL, verified idempotency, and queried it by repository/API TestClient. | `/data/video-analytics/media/evidence/c2_7_event_worker_persistence_20260607T223424` | Isolated one-message event-worker proof; live API HTTP not verified here. |
| C2.8 | `PASS_C2_8_API_EVIDENCE_DETAIL_READY` | `f514386d90ebbf703d2bea585388bb44f7cea20c` | API/TestClient and repository returned safe operator evidence detail for the persisted watchlist event. | `/data/video-analytics/media/evidence/c2_8_api_evidence_detail_20260607T225039` | Live API HTTP not required; no viewer rendering in this phase. |
| C2.10 | `PASS_C2_10_RUNTIME_API_VIEWER_READY` | `4335137d90bd3d4f64abf64c3d6d94ba40de291e` | Temporary live API HTTP and evidence-viewer runtime both returned watchlist evidence detail. Static operator report was generated. | `/data/video-analytics/media/evidence/c2_10_runtime_viewer_20260607T230426` | Temporary local API, not production API container; no long-running soak. |

## Final Demo Path

Input persisted event:

- event id:
  `b4cf4b6b-9d90-4282-9b7f-5e0c56e81a32`
- source event id:
  `c2_7:persisted:watchlist_hit:face:c2_post_savant_fps_probe:4:17854:1:4:c2_7_event_worker_persistence_20260607T223424`

API evidence responses:

- `/data/video-analytics/media/evidence/c2_10_runtime_viewer_20260607T230426/live_api_response_by_event_id.json`
- `/data/video-analytics/media/evidence/c2_10_runtime_viewer_20260607T230426/live_api_response_by_source_event_id.json`

Operator report:

- `/data/video-analytics/media/evidence/c2_10_runtime_viewer_20260607T230426/operator_watchlist_evidence.html`

Evidence bundle:

- `/data/video-analytics/media/evidence/c2_6r_redis_watchlist_20260607T221035`

Evidence-viewer URLs:

- `http://127.0.0.1:8090/api/bundles/c2_6r_redis_watchlist_20260607T221035`
- `http://127.0.0.1:8090/api/bundles/c2_6r_redis_watchlist_20260607T221035/annotations?source=sidecar`
- `http://127.0.0.1:8090/api/bundles/c2_6r_redis_watchlist_20260607T221035/media/raw_clip`

Audit files:

- `/data/video-analytics/media/evidence_audit/c2_6r_redis_watchlist_20260607T221035/index.html`
- `/data/video-analytics/media/evidence_audit/c2_6r_redis_watchlist_20260607T221035/contact_sheet.jpg`

Operator-facing values:

- `event_type=watchlist_hit`
- `source_observation_id=face:c2_post_savant_fps_probe:4:17854:1`
- `person_id=4`
- `external_person_id=test:c2_4:person`
- `watchlist_rule_id=c2_6r_test_watchlist_rule`
- `similarity=1.0`
- `threshold=0.99`
- `known_face_count=1`
- `watchlist_hit_count=1`
- `production_ready=true`
- `video_integrity_status=pass`

## Proven Data Boundaries

- Redis does not carry image bytes or crop bytes for the watchlist event proof.
- The watchlist event payload does not carry an embedding vector.
- API evidence response does not expose embedding vectors, image bytes, base64
  image payloads, or crop bytes.
- Geometry comes from the post-Savant sidecar.
- Identity comes from `match_results`, gallery embedding, and person records.
- Evidence summaries preserve:
  - `evidence_capture_mode=stable_post_savant_sink_time_crop`
  - `workaround_used=true`
  - `event_style_replay_job_passed=false`
- `fallback_used=false`.
- `legacy_used_for_visual_binding=false`.
- `allow_db_annotation_fallback=false`.
- `allow_legacy_annotation_fallback=false`.
- The viewer reads identity from sidecar/evidence files or API evidence detail;
  it does not query DB identity for visual binding.

## Known Limitations And Technical Debt

- Event-style Replay job failed and remains backlog.
- `stable_post_savant_sink_time_crop` is a workaround, not the final evidence
  capture mode.
- A fixed deterministic sample was used; the evidence crop is from the stable
  sink workaround rather than a passed event-style Replay output.
- Repeated clip use is expected and does not prove multi-event coverage.
- Long-running worker loop / soak is not proven.
- Broad recognition accuracy is not proven.
- C2.10 did not verify a live production API container; it verified live API
  HTTP through a temporary local Uvicorn process.
- Track id mismatch exists between the DB observation and evidence sidecar:
  DB `track_id=4` vs evidence/event `track_id=1` in earlier C2.6/C2.6R context.
  The primary identity join key is `source_observation_id`, not track id alone.
- C2.3Q still reports the known pose/keypoint warning class, manually accepted
  during the C2.3Q audit.
- The deterministic C2.4/C2.5/C2.6/C2.6R person is
  `external_person_id=test:c2_4:person`; this proves plumbing and semantics, not
  real-world recognition coverage.

## Next Path Options

### A. C2.9 - Long-Running Integrated Worker Smoke

Goal: prove a bounded multi-message path:

```text
face-worker -> event-worker -> DB/API
```

over N deterministic messages without running an infinite soak. Choose this when
the next engineering risk to close is worker orchestration and repeated-message
behavior.

### B. C2.11 - Demo Packaging

Goal: turn the current artifacts into a one-click demo checklist / operator
page. Choose this when the next priority is a repeatable stakeholder demo using
the already-proven C2.10 runtime evidence path.

### C. C2.3B-H - Replay Hardening

Goal: repair event-style Replay keyframe-safe time-domain evidence so the stable
sink workaround can be retired. Choose this when the next priority is removing
the largest technical caveat.

### D. C2.12 - Real External Face Enrollment / Real Video Watchlist Test

Goal: move from deterministic self-match to an external photo and a new video
segment. Choose this when the next priority is realism and recognition behavior
rather than pipeline semantics.

Recommended default next step: **C2.11 Demo Packaging**.

Reason: C2-R is a demo readiness rebaseline, C2.10 already made the current
watchlist evidence inspectable through live runtime endpoints, and C2.11 can
make the result repeatable without changing runtime semantics. For engineering
hardening after the demo path is packaged, prioritize C2.9 or C2.3B-H.
