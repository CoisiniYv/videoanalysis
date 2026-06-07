# Phase C2.12A - External Face Enrollment Rebuild for Reese / Finch

## Goal

C2.12A rebuilds external submitted-image gallery enrollment for Reese and
Finch using the current PostgreSQL database and the existing face-worker
external image registration path.

This phase registers only external face images. It does not generate new video
watchlist evidence, does not create watchlist rules, does not run Replay, and
does not change the Savant pipeline.

## Input Directory

`/data/video-analytics/media/face-registration`

Expected discovered files:

- `reese.jpg`
- `finch.jpg`

The C2.12A tool recursively inventories jpg, jpeg, png, webp, and bmp files and
infers identity from filename or parent directory.

## Registration Method

C2.12A uses the existing real external image enrollment service:

- `services/face-worker/register_face_image.py`
- `services/face-worker/app/image_face_registration.py`
- `services/face-worker/app/offline_face_embedder.py`

The path is:

```text
external image
-> YOLOv8-Face ONNX detection
-> 5-point landmark alignment
-> AdaFace ONNX embedding
-> persons
-> person_gallery_embeddings
```

Model paths:

- face detector:
  `/data/video-analytics/models/yolov8_face/yolov8n-face.onnx`
- AdaFace:
  `/data/video-analytics/models/adaface/adaface_ir50_webface4m.onnx`

The current machine has ONNXRuntime and OpenCV available. CPU provider fallback
is acceptable when CUDA provider is unavailable.

## Person IDs

Deterministic external IDs:

- Reese: `demo:f4_3:reese`
- Finch: `demo:f4_3:finch`

If these persons already exist, the tool reuses the person row. If an active
gallery embedding already exists for the same `source_image_path`, the tool
reuses that gallery row rather than duplicating it.

## Output

Output pattern:

`/data/video-analytics/media/evidence/c2_12a_external_enrollment_YYYYMMDDTHHMMSS`

Files:

- `enrollment_inventory.json`
- `enrollment_results.json`
- `db_persons_after.json`
- `db_gallery_after.json`
- `gallery_self_check.json`
- `unsafe_payload_scan.json`
- `enrollment_quality_report.md`
- `c2_12a_external_enrollment_summary.json`

Runtime output:

`/data/video-analytics/media/evidence/c2_12a_external_enrollment_20260608T003500`

Result marker:

`PASS_C2_12A_EXTERNAL_FACE_ENROLLMENT_READY`

Discovered images:

- Reese:
  `/data/video-analytics/media/face-registration/reese.jpg`
  - size: 159805 bytes
  - dimensions: 902x1174
  - selected for registration: true
- Finch:
  `/data/video-analytics/media/face-registration/finch.jpg`
  - size: 77329 bytes
  - dimensions: 1033x775
  - selected for registration: true

Registered gallery rows:

- Reese:
  - `person_id=5`
  - `external_person_id=demo:f4_3:reese`
  - `gallery_embedding_id=4`
  - `embedding_model=adaface`
  - `embedding_dim=512`
  - `embedding_norm=0.9999999933533453`
  - `quality=1.0`
  - `source_image_path=/data/video-analytics/media/face-registration/reese.jpg`
- Finch:
  - `person_id=6`
  - `external_person_id=demo:f4_3:finch`
  - `gallery_embedding_id=5`
  - `embedding_model=adaface`
  - `embedding_dim=512`
  - `embedding_norm=1.0000001000768493`
  - `quality=1.0`
  - `source_image_path=/data/video-analytics/media/face-registration/finch.jpg`

Self-check:

- Reese gallery self-search top1:
  `demo:f4_3:reese`, `gallery_embedding_id=4`, `similarity=1.0`
- Finch gallery self-search top1:
  `demo:f4_3:finch`, `gallery_embedding_id=5`, `similarity=1.0`

Provider info from registration output:

- detector providers: `CPUExecutionProvider`
- embedder providers: `CPUExecutionProvider`

## Verification

The smoke verifies:

- Reese and Finch persons exist and are active.
- Each has at least one active gallery embedding.
- `embedding_dim=512`.
- embedding norm is in `[0.90, 1.10]`.
- `embedding_model=adaface`.
- `source_image_path` points to the original external image.
- no image bytes, base64, crop bytes, or embedding vectors are stored in DB
  payload JSON.
- gallery self-check returns each person as top match with similarity >= 0.99.

## Safety Boundaries

- No fake embedding is allowed.
- No dev mock fixture is used.
- No Redis image bytes are used.
- No image bytes, crop bytes, or base64 payloads are stored in DB payload.
- Original registration images are not deleted and are not committed to git.
- No `watchlist_rules` table is created by this phase.
- Existing non-test data is not deleted.

## Limitations

- This is not a new video watchlist test.
- This is not a broad recognition accuracy test.
- `watchlist_rules` table is still absent unless an existing production
  migration/contract introduces it later.
- C2.12B is needed for new video / new segment watchlist evidence using Reese
  and Finch.
- Event-style Replay is still not passed.

## Verification Commands

```bash
python -m pytest harness/tests/test_c2_12a_external_face_enrollment_contract.py -q
python -m py_compile scripts/tools/register_c2_12_external_faces.py
git diff --check
bash -n scripts/smoke/current/check_c2_12a_external_face_enrollment.sh
bash scripts/smoke/current/check_c2_12a_external_face_enrollment.sh
```

The final smoke writes to PostgreSQL and `/data/video-analytics/media/evidence`.
It does not touch Redis, Replay, workers, or Savant.
