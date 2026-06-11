# Midterm Operator Portal Runtime Design

Date: 2026-06-11

This document records the current midterm operator portal design and the changes
landed for the 8090 single-entry operator workflow.

## Runtime Entry

Customer access uses one host port:

```text
http://0.0.0.0:8090/
```

The 8090 service is `services/evidence-viewer`. It now serves the Chinese
operator portal and keeps the existing evidence viewer API.

The internal API service remains private to the compose network:

```text
evidence-viewer:8090 -> api:8000
```

`api:8000` is not published to the host. Customers should not use a separate
8000 entrypoint.

## Portal Scope

The 8090 portal provides four customer-facing work areas:

- 摄像头管理: add/edit cameras through the existing `/api/v1/cameras` APIs.
  The camera workspace also exposes detection zones and visible algorithm-rule
  controls. Operators can enable/disable each algorithm rule and set the
  rule-level evidence policy, including `pre_seconds` and `post_seconds`.
- 人员与人脸: list people/gallery rows and upload still images for face
  registration through `/api/v1/people/register-face`.
- 告警证据: browse file-based evidence bundles through the existing 8090
  `/api/bundles` APIs, with front-end alarm category grouping and an
  operator-visible alarm machine time.
- 存储维护: preview storage cleanup jobs through the internal API proxy.

Camera and people calls are same-origin `/api/v1/*` requests from the browser.
The 8090 service proxies those requests to `http://api:8000` inside compose.
Evidence calls stay native to 8090.

The visible algorithm controls are not all equal runtime capabilities. The
current stage boundary is documented in
`docs/midterm_operator_algorithm_controls_runtime_status.md`: intrusion is the
fully implemented behavior evidence path, some behavior rules are partial or
config-only, and face algorithm switches are currently saved/exported but not
per-camera runtime gates.

## Alarm Machine Time

The 8090 evidence page must show when the alarm happened according to the
machine clock. This is intentionally separate from the video playback time and
from frame-relative PTS values.

The operator-visible fields are:

- evidence list: `报警 YYYY-MM-DD HH:mm:ss`
- evidence detail, 事件信息: `报警机器时间`

The native 8090 evidence API exposes the raw value and its source:

```json
{
  "alarm_machine_time": "2026-06-10T16:27:47.960000Z",
  "alarm_machine_time_source": "event.event_ts_ms"
}
```

Preferred source for new bundles is `metadata.json`:

```json
{
  "event": {
    "created_at": "2026-06-11T02:05:06+00:00",
    "alarm_machine_time": "2026-06-11T02:05:06+00:00",
    "alarm_machine_time_source": "events.created_at"
  }
}
```

For historical bundles, `services/evidence-viewer/app/evidence_index.py` falls
back only to values that look like real Unix epoch milliseconds. It may use
`event.event_ts_ms`, `event.timestamp_ms`, or a timestamp embedded in
`event.source_event_id`. Small video-relative timestamps are deliberately not
treated as machine time.

The browser formats the value in local time as `YYYY-MM-DD HH:mm:ss`. If no
trusted value exists, the detail field remains `-` and the list omits the alarm
time fragment.

## Face Registration Runtime

Manual face registration uses the real offline image path:

```text
uploaded still image
  -> API saves file under FACE_UPLOAD_ROOT
  -> libs/face_registration
  -> offline YOLOv8-Face detection
  -> offline AdaFace embedding
  -> PostgreSQL persons/person_gallery_embeddings
  -> gallery image URLs served through /media
```

The API forces manual-upload semantics:

- `external_person_id` and `name` are required.
- `source_type` is `manual_upload`.
- `keep_crop` defaults to true.
- upload limit defaults to 10 MB.
- dev/mock embedding fixtures are not exposed through the API.
- `allow_multiple_faces` is passed through to the real embedder path.

## API Image Strategy

The midterm API image uses:

```text
services/api/Dockerfile.face-runtime
services/api/requirements.face-runtime.txt
```

`Dockerfile.face-runtime` inherits from the local
`video-analytics-midterm-face-worker:latest` image. That base image already
contains ONNX Runtime, OpenCV, Numpy, psycopg, and pgvector from the face-worker
runtime.

The API layer only adds:

- FastAPI / uvicorn
- PyYAML
- python-multipart
- small system libraries required by the existing `opencv-python` wheel

This avoids reinstalling or re-downloading ONNX Runtime during API image builds.
On a fresh machine, build or provide `video-analytics-midterm-face-worker:latest`
before building the midterm API image.

## Compose Rules

The active compose file is:

```text
infra/docker-compose.midterm.yml
```

The API service must keep this behavior:

- build context: `../services/api`
- dockerfile: `Dockerfile.face-runtime`
- build arg `FACE_RUNTIME_IMAGE=video-analytics-midterm-face-worker:latest`
- `expose: ["8000"]`
- no host `ports` entry

The evidence-viewer service must keep this behavior:

- host port `8090:8090`
- `OPERATOR_API_BASE_URL=http://api:8000`
- depends on the internal `api` service

## Evidence Face Identity Semantics

`unknown_face` in the evidence UI does not mean "YOLOv8-Face identified a known
person but AdaFace was too slow and missed the same frame." The current pipeline
has two related but different data paths.

Frame-level annotations come from `security.frame_annotations`. A face object in
that stream is labeled as `unknown_face` by default because the frame annotation
stream intentionally does not carry embedding vectors or gallery-match results.
It is a visual face detection track, not a confirmed identity result.

Known identity is attached later only when a watchlist/live-search event has a
matched face observation. The production sidecar bridges the identity from the
event payload back onto the matching `source_observation_id` and marks that
trigger face as `known_face`.

So a face can appear as `unknown_face` when any of these are true:

- YOLOv8-Face detected the face, but AdaFace did not produce a valid embedding
  for that face.
- AdaFace produced an embedding, but `FaceReidGate` rejected it for quality,
  size, landmarks, missing track association, invalid norm, or throttle.
- The face observation was exported, but the face-worker did not find a gallery
  match above `WATCHLIST_THRESHOLD`.
- The face-worker match/event path lagged or failed, so there was no watchlist
  event payload for the media-worker to bridge into the evidence sidecar.
- The evidence sidecar is showing non-trigger faces in the clip window; by
  design only the trigger known face should be patched as known.

YOLOv8-Face can run more often or appear earlier than downstream identity work,
but "unknown" is not a final biometric classification by the detector. It is the
absence of a confirmed identity bridge for that displayed face object.

Current midterm timing-related defaults are:

```text
FACE_INFER_INTERVAL=2
FACE_EMBEDDING_INFER_INTERVAL=2
FACE_REID_MIN_INTERVAL_MS=1000
WATCHLIST_THRESHOLD=0.60
FRAME_ANNOTATION_EXPORT_MIN_INTERVAL_MS=0
```

If evidence shows many unknown faces while registration is expected to match,
check these in order:

1. Savant logs for `face_reid_gate` allowed/skipped counts and skip reasons.
2. Savant logs for `face_obs_export` exported/skipped counts.
3. Redis `security.face_observations` growth and pending entries.
4. face-worker logs for inserted observations and `watchlist_hit_emitted`.
5. PostgreSQL gallery rows for active primary embeddings and target IDs.
6. Evidence sidecar summary fields `known_face_count`,
   `unknown_face_count`, `trigger_known_face_present`, and
   `identity_scope_status`.

## Verification

The current verified checks for this design are:

```bash
python -m pytest \
  harness/tests/test_evidence_viewer_alarm_machine_time.py \
  harness/tests/test_c1g1b_operator_frontend_static.py \
  harness/tests/test_operator_face_registration_static.py \
  harness/tests/test_c1f4c_evidence_viewer_contract.py \
  harness/tests/test_midterm_deployment_contract.py \
  harness/tests/test_api_people_face_registration.py \
  harness/tests/test_c2_1c_viewer_face_overlay_visibility.py \
  harness/tests/test_c1m8b_viewer_source_enforcement.py \
  -q

python -m py_compile \
  services/evidence-viewer/app/main.py \
  services/evidence-viewer/app/config.py \
  services/evidence-viewer/app/evidence_index.py \
  services/media-worker/app/worker.py \
  services/api/app/main.py \
  services/api/app/routers/people.py \
  services/api/app/repositories/people.py \
  services/api/app/schemas/people.py

node --check services/evidence-viewer/app/static/operator.js
node --check services/evidence-viewer/app/static/evidence.js

docker compose -f infra/docker-compose.midterm.yml config
bash scripts/smoke/current/check_operator_camera_and_face_registration.sh
git diff --check
```

Expected runtime smoke marker when no test face image/model env is provided:

```text
PASS_OPERATOR_CAMERA_REGISTRATION_READY_FACE_REGISTRATION_SKIPPED
```

Expected full face-registration smoke marker when model paths and a test image
are provided:

```text
PASS_OPERATOR_CAMERA_AND_FACE_REGISTRATION_READY
```
