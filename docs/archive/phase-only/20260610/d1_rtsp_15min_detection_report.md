# D1 RTSP 15-Minute Detection Report

Phase D1 is a detection-only runtime check for the fixed RTSP source:

```text
rtsp://10.37.57.112:8554/live/1080movie
```

It runs the single media path:

```text
RTSP -> source-adapter -> replay-service -> savant-security
```

The run exports behavior/person track statistics from `security.events` and
face observation statistics from `security.face_observations`. It does not
start `clip-worker`, `media-worker`, or `video-file-sink`, and it does not
generate raw clips, annotated clips, or evidence bundles.

## Runtime Contract

- `input_type = rtsp`
- `input_uri = rtsp://10.37.57.112:8554/live/1080movie`
- `duration_seconds = 900` for the formal report
- `source_id = d1_rtsp_15min`
- `camera_id = cam_d1_rtsp_15min`
- `recording_enabled = false`
- `clip_generated = false`
- `local_file_used = false`
- `test_video_used = false`
- `second_rtsp_pull = false`
- `source_extraction_fallback = false`

The smoke supports `D1_DURATION_SECONDS=60` for debugging, but the formal D1
report must be generated with `D1_DURATION_SECONDS=900`.

## Docker Discipline

The smoke script detects Docker access first:

```text
DOCKER_ACCESS_OK
SUDO_DOCKER_REQUIRED
DOCKER_ACCESS_BLOCKED
```

After detection, runtime commands use `$DOCKER` and `$COMPOSE`. The default
startup is `up -d --no-build --force-recreate --pull never`. D1 does not define
worker build services, and Docker pull is not used.

## Outputs

The smoke writes generated inspection artifacts to:

```text
manual-inspection/d1_15min_detection_latest/
```

Generated files:

- `report.md`
- `summary.json`
- `people_tracks.csv`
- `people_tracks.json`
- `face_observations.csv`
- `face_observations.json`
- `gallery_hits.csv` and `gallery_hits.json` only when gallery/watchlist/live_search hits exist

These files are runtime artifacts and are not committed.

## Semantics

`face_observation` means a face observation was exported with face metadata and,
when available, an embedding. It is not a gallery recognition result. Gallery
recognition is available only when the run produces `watchlist_hit`,
`live_search_hit`, or `gallery_match`.

## Quality Fields

The report states whether these fields are available or unavailable:

- person bbox confidence
- keypoint confidence
- track_id
- bbox_source
- face bbox confidence
- landmarks
- face quality
- embedding_norm
- gallery similarity

Unavailable fields are reported as `field_status = unavailable`; the exporter
does not fabricate missing values.

## Validation

Run:

```bash
python3 -m pytest harness/tests/test_d1_rtsp_15min_detection_report_contract.py -q
D1_DURATION_SECONDS=900 bash scripts/smoke/check_d1_rtsp_15min_detection_report.sh
git diff --check
git status --short
```
