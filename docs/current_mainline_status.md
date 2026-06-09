# Current Mainline Status

## 2026-06-09 Active Runtime Clarification

The currently running evidence stack in this checkout is C2 replay-first:

- Branch: `c2/post-savant-poc`.
- Compose project: `c2-replay-first-dev`.
- Compose file: `infra/docker-compose.c2-replay-first-dev.yml`.
- Env file: `infra/env/c2-replay-first-dev.env`.
- Containers: `c2-replay-first-*`.
- Evidence viewer: host port `8090`.
- Database: workers default to the existing host PostgreSQL on
  `host.docker.internal:5432`, currently the separate `phase0-postgres`
  container.

C1 files remain available as C1 baselines and regression references. They are
not the active Docker runtime unless explicitly started with their own compose
file.

Date: 2026-05-29
Status: R2 consolidation snapshot.

## Summary

The current mainline has a working debug recognition path for local video and registered gallery faces, but it is not a production evidence or watchlist pipeline.

- F4.3 debug end-to-end recognition smoke: PASS.
- F4.3 production evidence pipeline: NOT DONE.

F4.3B proves that the recognition chain is viable on `testVideo/1080movie.mp4`; it does not prove production-grade realtime event evidence.

## Completed

- Savant local mp4 source adapter through the c1-official-adapter runtime.
- YOLO26-pose / tracker / ROI / intrusion behavior-rule path.
- YOLOv8-Face detector in `modules/savant_security`.
- AdaFace embedding in `modules/savant_security`.
- Redis `security.face_observations` export.
- `face-worker` ingestion into PostgreSQL `face_observations`.
- External image registration with `register_face_image.py`.
- `person_gallery_embeddings` storage.
- pgvector gallery matching.
- F4.3 40 minute debug recognition smoke:
  - 4412 face observations.
  - Finch best similarity 0.719549.
  - Reese best similarity 0.698301.
  - Finch/Reese both exceeded 0.50 in the debug smoke.

## Prototype or Debug

- F4.1 `find_person` raw clip fallback.
- F4.3B evidence export scripts.
- Manual visual snapshots.
- `annotated_hits.mp4`.
- `scripts/demo`.
- `top_candidates.html` and calibration reports.

## Not Complete

- Production `watchlist_hit`.
- Production `live_search`.
- Production realtime evidence generation.
- FastAPI person face upload.
- FastAPI find-person/search.
- Formal dashboard.
- Single GPU batch performance baseline.
- Dual T4 60-stream load test.

## Required Warnings

- Snapshots and videos are post-run exports, not realtime event evidence.
- `annotated_hits.mp4` is a debug clip, not an event-scoped production clip.
- No `watchlist_hit` or `live_search_hit` event chain exists.
- No FastAPI query entrypoint exists for the F4.3 recognition smoke.
- Media output lifecycle is not production-grade for recognition evidence.
- Thresholds need more data before production use.
- YOLO26-pose + YOLOv8-Face + AdaFace are statically present in the c1 official runtime, but runtime log verification should be repeated before performance testing.
