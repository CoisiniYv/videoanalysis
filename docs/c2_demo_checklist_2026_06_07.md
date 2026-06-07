# C2 Demo Checklist

Date: 2026-06-07

Use this checklist for the current C2 post-Savant watchlist evidence demo. It
does not require Replay, DB writes, Redis writes, worker loops, or container
restarts.

## Baseline

- [ ] Confirm the C2 branch / commit:
  - branch: `c2/post-savant-poc`
  - expected latest C2.10 commit:
    `4335137d90bd3d4f64abf64c3d6d94ba40de291e`
- [ ] Confirm the result marker:
  - `PASS_C2_10_RUNTIME_API_VIEWER_READY`
- [ ] Confirm the evidence-viewer health endpoint if runtime viewer is running:
  - `http://127.0.0.1:8090/health`

## Open Demo Artifacts

- [ ] Open operator report:
  - `/data/video-analytics/media/evidence/c2_10_runtime_viewer_20260607T230426/operator_watchlist_evidence.html`
- [ ] Open API response by DB event id:
  - `/data/video-analytics/media/evidence/c2_10_runtime_viewer_20260607T230426/live_api_response_by_event_id.json`
- [ ] Open API response by source event id:
  - `/data/video-analytics/media/evidence/c2_10_runtime_viewer_20260607T230426/live_api_response_by_source_event_id.json`
- [ ] Open evidence-viewer bundle manifest:
  - `http://127.0.0.1:8090/api/bundles/c2_6r_redis_watchlist_20260607T221035`
- [ ] Open sidecar annotations endpoint:
  - `http://127.0.0.1:8090/api/bundles/c2_6r_redis_watchlist_20260607T221035/annotations?source=sidecar`
- [ ] Open raw clip through viewer if needed:
  - `http://127.0.0.1:8090/api/bundles/c2_6r_redis_watchlist_20260607T221035/media/raw_clip`
- [ ] Open audit HTML if a static review is preferred:
  - `/data/video-analytics/media/evidence_audit/c2_6r_redis_watchlist_20260607T221035/index.html`
- [ ] Open audit contact sheet:
  - `/data/video-analytics/media/evidence_audit/c2_6r_redis_watchlist_20260607T221035/contact_sheet.jpg`

## Confirm Watchlist Evidence Fields

- [ ] `event_type=watchlist_hit`
- [ ] `source_observation_id=face:c2_post_savant_fps_probe:4:17854:1`
- [ ] `person_id=4`
- [ ] `external_person_id=test:c2_4:person`
- [ ] `watchlist_rule_id=c2_6r_test_watchlist_rule`
- [ ] `gallery_embedding_id=3`
- [ ] `match_result_id=6`
- [ ] `similarity=1.0`
- [ ] `threshold=0.99`
- [ ] `known_face_count=1`
- [ ] `watchlist_hit_count=1`
- [ ] `production_ready=true`
- [ ] `video_integrity_status=pass`

## Confirm Safety Boundaries

- [ ] `evidence_capture_mode=stable_post_savant_sink_time_crop`
- [ ] `workaround_used=true`
- [ ] `event_style_replay_job_passed=false`
- [ ] `fallback_used=false`
- [ ] `legacy_used_for_visual_binding=false`
- [ ] `allow_db_annotation_fallback=false`
- [ ] `allow_legacy_annotation_fallback=false`
- [ ] Unsafe payload scan:
  - `payload_has_embedding=false`
  - `payload_has_image_bytes=false`
  - `forbidden_key_paths=[]`
- [ ] Confirm no embedding vector is exposed in the watchlist event or API
  evidence response.
- [ ] Confirm no image/base64/crop bytes are exposed in the watchlist event or
  API evidence response.

## Confirm Limitations Are Visible

- [ ] Event-style Replay is not passed.
- [ ] The stable sink crop workaround is active.
- [ ] The demo uses a deterministic fixed sample and deterministic test person.
- [ ] The repeated clip is expected and does not prove multi-event coverage.
- [ ] Broad recognition accuracy is not proven.
- [ ] Long-running worker soak is not proven.
- [ ] C2.10 live API HTTP used temporary local Uvicorn, not a live production API
  container.
- [ ] Track id mismatch is documented:
  - DB observation `track_id=4`
  - evidence/event `track_id=1`
  - primary identity join key is `source_observation_id`
- [ ] C2.3Q pose/keypoint warning class remains manually accepted.

## Optional Read-Only Validation

Run:

```bash
bash scripts/smoke/current/check_c2_rebaseline_demo_artifacts.sh
```

The script checks only final artifact existence and JSON/HTML fields. It does
not start containers, run Replay, modify DB, modify Redis, or run worker loops.
