# F4.2 External Submitted Image -> Gallery Registration MVP

## Goal

F4.2 adds a new registration path:

```text
external submitted local image
  -> face detection
  -> landmarks
  -> face quality gate
  -> AdaFace 512-d embedding
  -> persons / person_gallery_embeddings write
```

This phase is specifically for external submitted image registration into `person_gallery_embeddings`.

## F4.2 vs F4.2b

**F4.2 (skeleton)** — completed:
- CLI, service, validation, storage, DB write path
- Dev mock fixture path for contract testing
- `NotImplementedRealImageEmbedder` placeholder that returns `REAL_IMAGE_EMBEDDING_UNAVAILABLE`

**F4.2b (real embedding)** — current:
- Real YOLOv8-Face offline inference via ONNXRuntime
- Real AdaFace offline inference via ONNXRuntime
- Face alignment using 5-point landmarks
- L2-normalized 512-d embedding
- Provider info output (CUDA/CPU)
- Image type validation (reject video files)
- Default uses `OfflineFaceEmbedder` when onnxruntime is available

## Explicit Scope

This phase is:

- external submitted image registration
- gallery creation / extension for a registered person
- write-side support for `persons` and `person_gallery_embeddings`

This phase is not:

- video observation enrollment
- trajectory query
- external video clip recognition
- live_search
- watchlist
- security.events
- API work
- Savant pipeline changes
- Redis producer changes

`enroll_gallery.py` already covers the separate path:

```text
face_observations -> gallery enrollment
```

F4.2 adds the missing path:

```text
external submitted image -> gallery enrollment
```

F4.3 will cover external video clip recognition, not this phase.

## Current State

The repository already has:

- `persons`
- `person_gallery_embeddings`
- `GalleryRepository.add_embedding(...)`
- embedding dimension and norm validation
- observation-based enrollment via `enroll_gallery.py`

The repository does not yet expose a ready offline Python runner for:

- YOLOv8-Face or equivalent face detector on local images
- 5-point landmark extraction on local images
- AdaFace offline embedding extraction on local images

So the current F4.2 implementation is an MVP skeleton with correct CLI, service, validation, storage, and DB write path reuse.

## Real Embedding Status

Current status:

```text
F4.2b: real embedding implemented
```

The `OfflineFaceEmbedder` class implements:

1. YOLOv8-Face offline inference (letterbox, decode, NMS, coordinate restore)
2. 5-point landmark face alignment to 112×112
3. AdaFace offline inference (BGR, (pixel-127.5)/127.5 normalization)
4. L2 normalization of 512-d embedding
5. Embedding dimension and norm validation

When onnxruntime is available, `register_face_image.py` uses `OfflineFaceEmbedder` by default.

If onnxruntime is NOT available, the code falls back to `NotImplementedRealImageEmbedder` which returns `REAL_IMAGE_EMBEDDING_UNAVAILABLE`.

No automatic fallback to `face_observations` is allowed.

## Dev Mock Path

For contract/dev-only dry runs, the CLI supports:

```bash
--dev-mock-embedding-fixture /absolute/path/to/fixture.json
```

Rules:

- default is off
- never used implicitly
- output marks `dev_mock_used=true`
- output marks `fallback_used=true`
- output marks `real_embedding_used=false`

This path is not production external image registration.

## Storage Policy

Registered crop storage follows:

```bash
MEDIA_ROOT="${MEDIA_ROOT:-/data/video-analytics/media}"
FACE_REGISTRATION_ROOT="${FACE_REGISTRATION_ROOT:-$MEDIA_ROOT/face_registration}"
```

If `/data/video-analytics/media` is unavailable or not writable, the code falls back to:

```text
./tmp/face_registration
```

The output explicitly reports:

- `storage_fallback_used`
- `storage_fallback_reason`
- `registered_crop_path`

## Image Type Validation

The CLI rejects video file types:

```text
.mp4, .mov, .avi, .mkv, .wmv, .flv, .webm
```

Error code: `IMAGE_FILE_TYPE_UNSUPPORTED`

Supported image types:

```text
.jpg, .jpeg, .png, .bmp, .webp
```

## ONNX Provider Configuration

Default provider spec: `CUDAExecutionProvider,CPUExecutionProvider`

If CUDAExecutionProvider is not available, falls back to CPUExecutionProvider.

CLI flags:
- `--face-detector-onnx` — path to YOLOv8-Face ONNX
- `--adaface-onnx` — path to AdaFace ONNX
- `--onnx-provider` — provider spec (comma-separated)

Environment variables:
- `YOLOV8_FACE_ONNX` — default detector path
- `ADAFACE_ONNX` — default embedder path

Provider info is included in JSON output:
- `detector_providers` — providers used by face detector
- `embedder_providers` — providers used by AdaFace embedder

## Data Write Contract

On successful registration, the flow:

1. creates or reuses `persons`
2. writes `person_gallery_embeddings`
3. uses `source_type = manual_upload`
4. stores `source_image_path`
5. stores `quality`
6. stores `face_bbox`
7. stores `landmarks`
8. stores `embedding_model = adaface`
9. validates `embedding_dim = 512`
10. validates embedding norm near `1.0`

It does not write:

- `face_observations`
- `match_results`
- `security.events`

## F4.2b Implementation Summary

F4.2b adds real external image -> AdaFace embedding:

**New files:**
- `services/face-worker/app/offline_face_embedder.py` — OfflineFaceEmbedder class
- `services/face-worker/app/onnx_runtime_utils.py` — ONNX session loading utilities
- `harness/tests/test_f4_2_offline_face_embedder_contract.py` — contract tests

**Modified files:**
- `services/face-worker/app/image_face_registration.py` — wire OfflineFaceEmbedder, add image type validation
- `services/face-worker/register_face_image.py` — add CLI args for model paths and provider
- `harness/tests/test_f4_2_external_face_registration_contract.py` — update contract tests
- `docs/phase_f4_2_external_face_registration_mvp.md` — this doc

**Key behaviors:**
1. Default uses `OfflineFaceEmbedder` when onnxruntime is available
2. Falls back to `NotImplementedRealImageEmbedder` if onnxruntime not installed
3. Rejects video file types (.mp4, .mov, .avi, etc.)
4. Outputs provider info (CUDA/CPU)
5. Validates embedding dim=512, norm in [0.90, 1.10]
6. No fallback to `face_observations`
