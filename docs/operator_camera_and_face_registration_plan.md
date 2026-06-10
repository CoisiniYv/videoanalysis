# Operator Camera and Face Registration Plan

Status: implementation baseline. This document started as the reviewed plan and
now records the target design plus the first implementation slice.

Date: 2026-06-09

## Implementation Snapshot

The first slice is implemented in the repo:

- `infra/docker-compose.midterm.yml` now defines an internal `api` service on
  compose port `8000`; it is not published to the host.
- The customer-facing operator portal is served by `services/evidence-viewer`
  on host port `8090`.
- The 8090 portal has top-level Chinese views for camera management, people/face
  registration, and evidence review.
- Camera registration/editing stays on the existing `/api/v1/cameras` APIs.
- People APIs were added under `/api/v1/people`.
- `POST /api/v1/people/register-face` accepts multipart image uploads, requires
  `external_person_id` and `name`, forces `source_type="manual_upload"`, keeps
  crops by default, enforces the 10 MB default upload limit, and does not accept
  a dev mock fixture.
- Face registration code is shared through `libs/face_registration`.
- `allow_multiple_faces` and `quality_threshold` are now forwarded into the
  real offline YOLOv8-Face/AdaFace embedder.
- `scripts/smoke/current/check_operator_camera_and_face_registration.sh` checks
  8090 `/health`, the 8090 portal page, deterministic camera create/update
  through `/api/v1/*`, camera config, and optionally real face upload when
  model/image env vars are present.
- `db/migrations/012_operator_camera_schema_compat.sql` provides a
  non-destructive compatibility migration for long-lived databases that still
  have the original placeholder camera tables from `001_init.sql`.
- The midterm API image uses `services/api/Dockerfile.face-runtime`, which
  inherits from `video-analytics-midterm-face-worker:latest` to reuse the
  already-installed ONNX Runtime/OpenCV/Numpy layer instead of reinstalling ORT
  during API builds.

Deferred items:

- Gallery deactivation/delete APIs.
- Runtime Savant apply after camera DB edits.
- Visual ROI editor.
- Auth/permissions.

## Goal

Turn the existing operator surface into a single operational entrypoint for:

1. Adding and editing cameras, zones, alert policy, and algorithm rules.
2. Registering people/faces from uploaded still images.
3. Running the operator/API service as part of the current deployment topology.

The repo already has the camera operator page and the backend camera APIs. The
missing work is to make that page available in the current runtime and extend
it with a People/Face Registration workflow backed by the existing real
YOLOv8-Face/AdaFace registration path.

## Non-Goals

- Do not change the Savant model chain.
- Do not add a mock/dev face registration path to operator/API.
- Do not fall back to `face_observations` for manual face registration.
- Do not store image bytes in PostgreSQL JSON payloads or Redis streams.
- Do not expose a separate customer-facing 8000 operator port in the midterm
  deployment.
- Do not auto-apply camera DB edits to a running Savant module in this step.

## Current State

### Operator Page Exists

The repo already has a lightweight operator page:

- Current customer route: `GET /` on the 8090 operator portal
- Legacy/internal API route: `GET /operator`
- Static assets:
  - `services/api/app/static/operator/index.html`
  - `services/api/app/static/operator/app.js`
  - `services/api/app/static/operator/style.css`
- The customer-facing assets now live under `services/evidence-viewer/app/static`.
- Camera and people API requests use same-origin `/api/v1/*` on 8090; the
  evidence-viewer service proxies those calls to the internal `api:8000`
  container.
- The older `services/api/app/static/operator/*` page remains as an internal
  compatibility surface, not the midterm customer entrypoint.

The page is currently a camera/zone/rule workbench. It has:

- camera list
- `New` camera button
- camera form
- `Save`, `Enable`, `Disable`
- FPS policy fields
- alert policy fields
- zone JSON editor
- algorithm rule templates
- full config JSON viewer

### Camera APIs Exist

`services/api/app/routers/cameras.py` already exposes:

- `GET /api/v1/cameras`
- `POST /api/v1/cameras`
- `GET /api/v1/cameras/{camera_id}`
- `PUT /api/v1/cameras/{camera_id}`
- `POST /api/v1/cameras/{camera_id}/enable`
- `POST /api/v1/cameras/{camera_id}/disable`
- `GET /api/v1/cameras/{camera_id}/config`
- `GET /api/v1/cameras/config/export`
- zone CRUD under `/api/v1/cameras/{camera_id}/zones`
- rule CRUD under `/api/v1/cameras/{camera_id}/rules`

The existing operator JS uses real `/api/v1/cameras` endpoints and no mock data.

### Camera Page Runtime Gap

The original gap was that the camera page existed under `services/api`, while
the active customer runtime exposed only the evidence viewer on `8090`. The
target runtime is now a single 8090 entrypoint: evidence-viewer serves the
operator portal and proxies camera/people API calls to the internal API
container.

### Face Registration Backend Exists

The backend/CLI path for external still-image face registration exists:

- CLI entrypoint: `services/face-worker/register_face_image.py`
- Registration service: `services/face-worker/app/image_face_registration.py`
- Real offline embedder: `services/face-worker/app/offline_face_embedder.py`
- DB repositories:
  - `services/face-worker/app/person_repository.py`
  - `services/face-worker/app/gallery_repository.py`

The CLI accepts model path/provider options and registration options including:

- `--image`
- `--external-person-id`
- `--name`
- `--person-id`
- `--description`
- `--is-primary`
- `--quality-threshold`
- `--allow-multiple-faces`
- `--keep-crop`
- `--face-detector-onnx`
- `--adaface-onnx`
- `--onnx-provider`

The registration write targets are:

- `persons`
- `person_gallery_embeddings`

No first-pass schema migration is needed for face registration.

### Face Registration API/UI Gap

There is no API router for people/faces/gallery upload. `services/api/app/main.py`
currently includes events, cameras, algorithms, and websocket alerts, but no
people/face router.

The operator page has no People or Face Registration view. It cannot currently:

- list people
- list gallery embeddings
- upload a face image
- register a person from an image
- mark uploaded gallery rows primary
- show registration errors/results

### Confirmed `allow_multiple_faces` Bug

`OfflineFaceEmbedder.extract()` supports:

```python
extract(image_path, allow_multiple_faces=False, quality_threshold=0.65)
```

But `OfflineFaceEmbedderAdapter.extract()` currently calls:

```python
result = self._embedder.extract(image_path)
```

So `RegistrationRequest.allow_multiple_faces` does not reach the real embedder.
The embedder can reject a multi-face image before the registration service gets
to select the best candidate.

This must be fixed before exposing the feature to operators.

## Target Operator Shape

Keep `/operator` as one operational workbench served by `services/api`.

Add top-level navigation:

- `Cameras`
- `People`

The `Cameras` view should preserve the current page behavior. The `People` view
should be new and should reuse the same compact operator style.

### Cameras View

Keep and harden the existing camera workflow:

- list cameras
- create camera
- edit camera
- enable/disable camera
- edit FPS policy
- edit alert policy
- edit zones
- edit algorithm rules
- view full aggregated config

Important behavior:

- `Save` for a new camera continues to call `POST /api/v1/cameras`.
- `Save` for an existing camera continues to call `PUT /api/v1/cameras/{id}`.
- No mock data.
- No evidence-viewer dependency.
- No runtime Savant apply in this step.

Recommended UI cleanup while adding tabs:

- Do not rewrite the camera editor from scratch.
- Wrap the current three-pane camera layout under a `Cameras` panel.
- Keep existing element ids where practical to reduce JS churn.
- Add a clear API/runtime status indicator for `/api/v1/cameras`.

### People View

Add a new People/Face Registration panel:

- left: people list and search
- middle: face registration form
- right: selected person gallery and latest registration result

Registration fields:

- image file input
- existing `person_id` selector, or new `external_person_id` plus `name`
- description
- `is_primary`
- `allow_multiple_faces`
- `quality_threshold`, default `0.65`
- `keep_crop`, default enabled for operator uploads
- optional operator/created-by field

Behavior:

- Use `FormData` and multipart upload.
- Do not use JSON `Content-Type` for upload requests.
- Disable submit while inference is running.
- Surface model, quality, multiple-face, and DB errors clearly.
- On success:
  - refresh people list
  - select the registered person
  - show `person_id`, `gallery_embedding_id`, quality, bbox, embedding norm,
    provider info, and whether the person was reused

## API Plan

### Existing Camera API

Keep the current camera router as the source of truth:

- `services/api/app/routers/cameras.py`
- `services/api/app/schemas/cameras.py`
- `services/api/app/repositories/cameras.py`

No new camera router should be introduced.

Add or update tests only where the unified operator tabs require static asset
changes:

- `harness/tests/test_c1g1b_operator_frontend_static.py`
- possibly a new `harness/tests/test_operator_unified_static.py`

### New People API

Add:

- `services/api/app/routers/people.py`
- `services/api/app/schemas/people.py`
- `services/api/app/repositories/people.py`

Register the router in:

- `services/api/app/main.py`

Prefix:

- `/api/v1/people`

#### `GET /api/v1/people`

Purpose: list active people for operator selection.

Query params:

- `include_inactive=false`
- `q`
- `limit=50`
- `offset=0`

Response data:

```json
{
  "people": [
    {
      "person_id": 123,
      "name": "Reese",
      "external_person_id": "demo:midterm:reese",
      "description": null,
      "is_active": true,
      "active_gallery_count": 2,
      "primary_gallery_embedding_id": 456,
      "created_at": "...",
      "updated_at": "..."
    }
  ],
  "total": 1,
  "limit": 50,
  "offset": 0
}
```

#### `GET /api/v1/people/{person_id}`

Purpose: show one person plus gallery metadata.

Do not return embedding vectors.

Response data:

```json
{
  "person": {
    "person_id": 123,
    "name": "Reese",
    "external_person_id": "demo:midterm:reese",
    "is_active": true
  },
  "gallery": [
    {
      "gallery_embedding_id": 456,
      "source_type": "manual_upload",
      "source_image_path": "...",
      "embedding_model": "adaface",
      "model_version": null,
      "embedding_dim": 512,
      "embedding_norm": 1.0,
      "quality": 0.91,
      "face_bbox": [10.0, 20.0, 80.0, 80.0],
      "landmarks": [[...]],
      "is_primary": true,
      "is_active": true,
      "created_at": "...",
      "updated_at": "..."
    }
  ]
}
```

#### `POST /api/v1/people/register-face`

Purpose: register one external submitted still image into
`person_gallery_embeddings`.

Request type:

- `multipart/form-data`

Fields:

- `image`: required file upload
- `external_person_id`: optional string
- `name`: optional string
- `person_id`: optional integer
- `description`: optional string
- `is_primary`: optional boolean, default `false`
- `quality_threshold`: optional float, default `0.65`
- `allow_multiple_faces`: optional boolean, default `false`
- `keep_crop`: optional boolean, default `true`
- `operator`: optional string for `created_by`

Validation:

- require one of `person_id`, `external_person_id`, or `name`
- reject unsupported extensions
- reject video file extensions
- reject empty upload
- enforce upload size limit
- force `source_type="manual_upload"`
- never expose or accept `dev_mock_embedding_fixture`
- never accept model path overrides from operator clients

Success response should reuse the existing API envelope:

```json
{
  "data": {
    "status": "REGISTERED",
    "mode": "external_image",
    "person_id": 123,
    "person_reused": false,
    "external_person_id": "demo:midterm:reese",
    "name": "Reese",
    "gallery_embedding_id": 456,
    "is_primary": true,
    "face_bbox": [10.0, 20.0, 80.0, 80.0],
    "quality": 0.91,
    "embedding_model": "adaface",
    "embedding_dim": 512,
    "embedding_norm": 1.0,
    "source_image_path": "/data/video-analytics/media/face_uploads/...",
    "registered_crop_path": "/data/video-analytics/media/face_registration/...",
    "source_type": "manual_upload",
    "real_embedding_used": true,
    "dev_mock_used": false,
    "fallback_used": false,
    "detector_providers": ["CPUExecutionProvider"],
    "embedder_providers": ["CPUExecutionProvider"]
  },
  "error": null,
  "request_id": "..."
}
```

Failure response should preserve the registration error code:

```json
{
  "data": null,
  "error": {
    "message": "Detected 2 faces; enable allow_multiple_faces",
    "code": 400,
    "registration_error_code": "MULTIPLE_FACES_DETECTED"
  },
  "request_id": "..."
}
```

Status mapping:

- `400`: invalid request, unsupported file, no face, multiple faces, too small,
  missing landmarks, quality too low
- `404`: person not found
- `409`: external person conflict or primary constraint conflict
- `500`: model unavailable, DB unavailable, unexpected inference failure

#### Optional Later Endpoints

Phase 2, not required for first upload:

- `POST /api/v1/people/{person_id}/gallery/{gallery_embedding_id}/primary`
- `DELETE /api/v1/people/{person_id}/gallery/{gallery_embedding_id}`

## Face Registration Code Architecture

### Preferred

Extract face registration domain code into a shared package:

- `libs/face_registration/`

Move or wrap:

- `image_face_registration.py`
- `offline_face_embedder.py`
- `onnx_runtime_utils.py`
- `face_image_preprocess.py`
- `person_repository.py`
- `gallery_repository.py`

Then:

- keep `register_face_image.py` as a CLI wrapper
- keep face-worker compatibility imports if needed
- let `services/api` import the shared package without `app.*` package-name
  collision

### Rejected Temporary Alternative

API saves the uploaded file and calls the existing CLI as a subprocess. This was
rejected for the midterm entrypoint because request/error handling is weaker and
it still requires the API runtime to carry the face inference dependencies.

## Upload Storage Plan

Server-side upload root:

- env: `FACE_UPLOAD_ROOT`
- default: `${MEDIA_ROOT}/face_uploads`
- host target: `/data/video-analytics/media/face_uploads`

Registration crop root:

- existing env: `FACE_REGISTRATION_ROOT`
- default: `${MEDIA_ROOT}/face_registration`

Upload handling:

1. Validate size and extension.
2. Write to temp file under upload root.
3. Rename to a safe deterministic filename:
   - timestamp
   - sanitized original stem
   - short UUID
   - extension
4. Close the file before OpenCV reads it.
5. Pass the saved path as `RegistrationRequest.image_path`.
6. Keep the saved file as `source_image_path`.
7. Do not store image bytes in DB, Redis, logs, or JSON responses.

## Runtime and Compose Plan

The current customer entrypoint is the 8090 operator portal. `services/api`
exists only as an internal compose service used by the 8090 proxy.

Minimum internal API service:

- build context: `../services/api`
- dockerfile: `Dockerfile.face-runtime`
- base runtime image: `video-analytics-midterm-face-worker:latest`
- expose only: compose port `8000`
- env:
  - `DATABASE_URL`
  - `MEDIA_ROOT`
  - `REDIS_URL` if websocket alerts remain enabled
- depends on Redis; PostgreSQL is reached through `DATABASE_URL`

Extra API requirements for face registration:

- `video-analytics-midterm-face-worker:latest` already provides ONNX Runtime,
  OpenCV, and Numpy.
- `services/api/requirements.face-runtime.txt` only adds the API/web packages:
  `fastapi`, `uvicorn`, `psycopg`, `redis`, `PyYAML`, `python-multipart`, and
  `pgvector`.
- `Dockerfile.face-runtime` adds the small system libraries needed by the
  existing `opencv-python` wheel.
- mounts:
  - `/data/video-analytics/media:/data/video-analytics/media:rw`
  - `/data/video-analytics/models:/data/video-analytics/models:ro`
- env:
  - `MEDIA_ROOT=/data/video-analytics/media`
  - `FACE_UPLOAD_ROOT=/data/video-analytics/media/face_uploads`
  - `FACE_REGISTRATION_ROOT=/data/video-analytics/media/face_registration`
  - `YOLOV8_FACE_ONNX=/data/video-analytics/models/yolov8_face/yolov8n-face.onnx`
  - `ADAFACE_ONNX=/data/video-analytics/models/adaface/adaface_ir50_webface4m.onnx`
  - `FACE_REGISTRATION_ONNX_PROVIDER=CUDAExecutionProvider,CPUExecutionProvider`

Decision taken for this slice:

- `infra/docker-compose.midterm.yml` is the operator/API runtime entrypoint.

## Implementation Steps

### Step 0: Restore Operator/API Runtime

Goal: make the existing camera registration workflow reachable through the 8090
portal.

Files:

- `infra/docker-compose.midterm.yml` or confirmed current compose
- possibly `infra/env/midterm.env`

Work:

1. Add the internal API service to current compose.
2. Ensure the 8090 portal root serves the camera UI.
3. Ensure 8090 `/api/v1/cameras` proxies to the configured internal API and
   database.
4. Do not change the camera page behavior yet.

Acceptance:

- `curl http://0.0.0.0:8090/health` returns ok.
- `curl http://0.0.0.0:8090/` returns HTML containing the camera UI.
- `curl http://0.0.0.0:8090/api/v1/cameras` returns the API envelope.

### Midterm: Preserve and Test Existing Camera Workflow

Goal: protect the current camera add/edit behavior before adding People.

Files:

- `services/api/app/static/operator/index.html`
- `services/api/app/static/operator/app.js`
- `services/api/app/static/operator/style.css`
- `harness/tests/test_c1g1b_operator_frontend_static.py`
- optionally `harness/tests/test_operator_unified_static.py`

Work:

1. Add top-level operator tabs while keeping the current camera DOM intact.
2. Keep camera API calls unchanged.
3. Add static tests that the `Cameras` tab contains:
   - `new-camera`
   - `camera-form`
   - `save-camera`
   - `/api/v1/cameras`
4. Verify no references to 8090/evidence-viewer are introduced.

Acceptance:

- Existing operator static tests pass.
- Camera create/edit code paths still reference real camera APIs.

### Step 2: Fix Face `allow_multiple_faces`

Files:

- `services/face-worker/app/image_face_registration.py`
- `harness/tests/test_f4_2_offline_face_embedder_contract.py`
- possibly `harness/tests/test_f4_2_external_face_registration_contract.py`

Work:

1. Update the `RealImageEmbedder` protocol so `extract()` accepts:
   - `allow_multiple_faces`
   - `quality_threshold`
2. Update `DevMockEmbeddingFixtureEmbedder.extract()` and
   `NotImplementedRealImageEmbedder.extract()` signatures.
3. Update `OfflineFaceEmbedderAdapter.extract()` to forward:

```python
result = self._embedder.extract(
    image_path,
    allow_multiple_faces=allow_multiple_faces,
    quality_threshold=quality_threshold,
)
```

4. Update `register_external_image()` to call `active_embedder.extract()` with
   request values.
5. Keep `_select_candidate()` for fixture/future multi-candidate embedders.

Acceptance:

- A fake adapter test proves the keywords are passed.
- Existing face registration contract tests still pass.

### Step 3: Add People API

Files:

- `services/api/app/main.py`
- `services/api/app/routers/people.py`
- `services/api/app/schemas/people.py`
- `services/api/app/repositories/people.py`
- `services/api/requirements.txt`

Work:

1. Add people list/detail endpoints.
2. Add multipart face upload endpoint.
3. Add upload storage helper.
4. Force real registration settings and `manual_upload`.
5. Map registration failures to API errors.
6. Do not return embedding vectors.

Acceptance:

- API tests pass with fake registration service and temp upload root.
- Upload endpoint rejects video extensions.
- Upload endpoint never exposes dev mock args.

### Step 4: Add People Tab to Operator

Files:

- `services/api/app/static/operator/index.html`
- `services/api/app/static/operator/app.js`
- `services/api/app/static/operator/style.css`
- `harness/tests/test_operator_unified_static.py` or
  `harness/tests/test_operator_face_registration_static.py`

Work:

1. Add `People` tab next to `Cameras`.
2. Add people list/search.
3. Add registration form with file upload.
4. Add gallery metadata panel.
5. Add result/error panel.
6. Use `FormData` for `POST /api/v1/people/register-face`.
7. Keep current camera tab behavior intact.

Acceptance:

- Static tests confirm both tabs exist.
- JS references real `/api/v1/cameras` and `/api/v1/people` endpoints.
- JS uses `FormData` for upload.
- JS does not reference mock registration or dev mock embedding.

### Step 5: Runtime Smoke

Add:

- `scripts/smoke/current/check_operator_camera_and_face_registration.sh`

Smoke steps:

1. Check 8090 `/health`.
2. Check 8090 `/`.
3. Check camera list endpoint.
4. Create or update a deterministic test camera.
5. Fetch that camera config.
6. If model/env image inputs are present, upload a test face image.
7. Verify returned gallery row in DB.
8. Print pass marker:

```text
PASS_OPERATOR_CAMERA_AND_FACE_REGISTRATION_READY
```

If model files or test image are missing, the smoke may report a clear partial
marker for camera-only readiness:

```text
PASS_OPERATOR_CAMERA_REGISTRATION_READY_FACE_REGISTRATION_SKIPPED
```

## Test Plan

Targeted tests after implementation:

```bash
python -m pytest \
  harness/tests/test_c1g1b_operator_frontend_static.py \
  harness/tests/test_f4_2_offline_face_embedder_contract.py \
  harness/tests/test_f4_2_external_face_registration_contract.py \
  harness/tests/test_api_people_face_registration.py \
  harness/tests/test_operator_face_registration_static.py \
  -q
```

Compile checks:

```bash
python -m py_compile \
  services/face-worker/register_face_image.py \
  services/face-worker/app/image_face_registration.py \
  services/face-worker/app/offline_face_embedder.py \
  services/api/app/main.py \
  services/api/app/routers/cameras.py \
  services/api/app/routers/people.py \
  services/api/app/repositories/people.py \
  services/api/app/schemas/people.py
```

Compose check:

```bash
docker compose -f infra/docker-compose.midterm.yml config >/tmp/midterm.compose.rendered.yml
```

Smoke syntax:

```bash
bash -n scripts/smoke/current/check_operator_camera_and_face_registration.sh
```

Runtime smoke:

```bash
bash scripts/smoke/current/check_operator_camera_and_face_registration.sh
```

Repo hygiene:

```bash
git diff --check
git status --short
```

## Expected File Changes

Runtime/API:

- `infra/docker-compose.midterm.yml`
- `infra/env/midterm.env` if env defaults are centralized
- `services/api/requirements.txt`
- `services/api/Dockerfile`
- `services/api/Dockerfile.face-runtime`
- `services/api/requirements.face-runtime.txt`
- `services/api/app/main.py`
- `services/api/app/routers/people.py`
- `services/api/app/schemas/people.py`
- `services/api/app/repositories/people.py`

Face backend:

- `services/face-worker/app/image_face_registration.py`
- possibly shared package files under `libs/face_registration/`
- compatibility wrappers under `services/face-worker/app/` if shared package is
  accepted

Operator frontend:

- `services/evidence-viewer/app/static/index.html`
- `services/evidence-viewer/app/static/operator.js`
- `services/evidence-viewer/app/static/evidence.js`
- `services/evidence-viewer/app/static/style.css`

Tests/smoke:

- `harness/tests/test_c1g1b_operator_frontend_static.py`
- `harness/tests/test_api_people_face_registration.py`
- `harness/tests/test_operator_face_registration_static.py`
- `scripts/smoke/current/check_operator_camera_and_face_registration.sh`

Docs:

- update `docs/c1g1b_operator_frontend_enhancement.md` after implementation
- update this plan or add a completion report after implementation

## Open Review Decisions

1. `infra/docker-compose.midterm.yml` is used as the canonical operator/API
   runtime for this slice.
2. Face registration code was extracted into `libs/face_registration`; no CLI
   subprocess is used by the API.
3. `external_person_id` is required for operator-created face registrations.
4. `allow_multiple_faces` is sent as a hidden/internal operator default in the
   customer UI, not exposed as a normal customer field.
5. Uploads default to `keep_crop=true`.
6. Upload max size defaults to 10 MB via `FACE_UPLOAD_MAX_BYTES`.
7. Gallery deactivation is deferred.

## Recommended First Slice

Implement in this order:

1. Restore/start internal `services/api` in the current runtime so the 8090
   portal can proxy camera and people API calls.
2. Add tests around the existing camera tab before changing the operator layout.
3. Add top-level `Cameras` and `People` tabs, preserving camera behavior.
4. Fix `allow_multiple_faces` propagation in the real face embedder adapter.
5. Add People API list/detail/upload endpoints.
6. Add People tab UI and upload workflow.
7. Add combined camera + face runtime smoke.

This sequence protects the working camera registration page while extending the
same operator surface for face registration.
