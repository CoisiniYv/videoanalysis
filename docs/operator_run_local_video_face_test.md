# Operator: Run Local Video Face Test

Status: R2 operator draft.

## Purpose

Run a local mp4 through the c1-official-adapter runtime to validate the face branch and registered-person debug recognition. This is a debug smoke, not production watchlist/live_search.

## Scope

- Uses local mp4 source adapter.
- Does not use RTSP.
- Reuses `infra/docker-compose.c1-official-adapter.yml`.
- Reuses `scripts/runtime/camera_source_controller.py`.
- Writes evidence under `/data/video-analytics/media` when possible.

## F4.3A Observation Smoke

F4.3A validates:

```text
local mp4 -> video_loop.sh source adapter -> ZMQ -> savant-security
  -> YOLOv8-Face -> AdaFace -> Redis security.face_observations
  -> face-worker -> PostgreSQL face_observations
```

Example:

```bash
F4_3A_VIDEO_PATH="$PWD/testVideo/1080movie.mp4" \
F4_3A_SOURCE_ID="f4_3a_1080movie_verify_$(date +%s)" \
F4_3A_CAMERA_ID="cam_f4_3a_verify" \
F4_3A_WAIT_SECONDS=120 \
ADAFACE_ONNX="/data/video-analytics/models/adaface/adaface_ir50_webface4m.onnx" \
bash scripts/smoke/check_f4_3a_local_video_face_observation.sh
```

The script must show `ADAPTER_LOCATION=/testVideo/1080movie.mp4`, not `allface.mp4`.

## F4.3B Debug Recognition Smoke

F4.3B validates registered-person matching against existing Finch/Reese gallery and produces a debug evidence package:

```bash
F4_3B_VIDEO_PATH="$PWD/testVideo/1080movie.mp4" \
F4_3B_SOURCE_ID="f4_3b_1080movie_$(date +%s)" \
F4_3B_CAMERA_ID="cam_f4_3b" \
F4_3B_RUN_SECONDS=180 \
F4_3B_TARGET_EXTERNAL_IDS="demo:f4_3:finch,demo:f4_3:reese" \
F4_3B_MATCH_THRESHOLD=0.50 \
F4_3B_MAX_SNAPSHOTS=20 \
MEDIA_ROOT="/data/video-analytics/media" \
bash scripts/smoke/check_f4_3b_registered_person_video_recognition.sh
```

For a longer debug run, set `F4_3B_RUN_SECONDS=2400`.

## Outputs

Default output root:

```text
/data/video-analytics/media/f4_3b_recognition_evidence/
```

Run directories contain:

- `summary.json`
- `hits.csv`
- `top_candidates.html`
- `snapshots/*.jpg`
- `clips/annotated_hits.mp4` or candidate clip for no-hit runs
- `run.log`

If `/data/video-analytics/media` is missing or not writable, scripts may fall back to `./tmp/...`, but they must print:

```text
storage_fallback_used=true
storage_fallback_reason=...
```

## How to Inspect

- Open `summary.json` for counts, thresholds, and top candidates.
- Open `hits.csv` for sortable hit rows.
- Open `top_candidates.html` in a browser for manual candidate review.
- Open `clips/annotated_hits.mp4` for a debug annotated clip.

## Limits

- F4.3B is debug evidence, not a production evidence pipeline.
- Snapshots and video are post-run exports, not realtime event evidence.
- No `watchlist_hit` or `live_search_hit` events are generated.
- No FastAPI search endpoint is involved.
- Thresholds still need more calibration.

Do not commit `face/`, `testVideo/`, `manual-inspection/`, `/data` output, or generated media.

