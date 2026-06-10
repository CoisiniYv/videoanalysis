# Operator: Register a Face

Status: R2 operator draft.

## Purpose

Register a person from one clear image using offline YOLOv8-Face detection and AdaFace embedding. The result is written to `person_gallery_embeddings`.

## Image Requirements

- One person in the image.
- Clear frontal or near-frontal face.
- Face should be large enough for reliable landmarks.
- Avoid heavy blur, occlusion, extreme profile, and strong lighting mismatch.
- Keep test photos in `face/` or another local-only directory, but do not commit `face/`.

## Environment

Use the actual model paths:

```bash
cd ~/video-analytics
export DATABASE_URL="${DATABASE_URL:-postgresql://video:video@localhost:5438/video_analytics}"
export YOLOV8_FACE_ONNX="/data/video-analytics/models/yolov8_face.onnx"
export ADAFACE_ONNX="/data/video-analytics/models/adaface/adaface_ir50_webface4m.onnx"
```

Do not use `/data/video-analytics/models/adaface.onnx`; that default path is not the active model file.

## Register

```bash
python3 services/face-worker/register_face_image.py \
  --image face/reese.jpg \
  --external-person-id demo:f4_3:reese \
  --name "Reese" \
  --is-primary \
  --output-json
```

Expected result:

- `real_embedding_used=true`
- `dev_mock_used=false`
- `source_type=manual_upload`
- `source_observation_id=NULL`
- `embedding_model=adaface`
- `embedding_dim=512`
- `embedding_norm` near 1.0

## SQL Verification

```bash
psql "$DATABASE_URL" -c "
SELECT
  p.id AS person_id,
  p.external_person_id,
  p.name,
  pge.id AS gallery_embedding_id,
  pge.source_type,
  pge.source_image_path,
  pge.source_observation_id,
  pge.embedding_model,
  pge.embedding_dim,
  ROUND(pge.embedding_norm::numeric, 6) AS embedding_norm,
  pge.is_primary,
  pge.is_active,
  pge.payload->>'real_embedding_used' AS real_embedding_used,
  pge.payload->>'dev_mock_used' AS dev_mock_used
FROM persons p
JOIN person_gallery_embeddings pge ON pge.person_id = p.id
WHERE p.external_person_id = 'demo:f4_3:reese'
ORDER BY pge.id DESC
LIMIT 5;
"
```

## Common Troubleshooting

- `MODEL_FILE_NOT_FOUND`: check `YOLOV8_FACE_ONNX` and `ADAFACE_ONNX`.
- `NO_FACE_DETECTED`: use a clearer image or crop closer.
- `MULTIPLE_FACES_DETECTED`: crop to one person.
- `FACE_TOO_SMALL`: use a higher-resolution or tighter crop.
- `QUALITY_TOO_LOW`: use a cleaner frontal image.
- `DATABASE_CONNECTION_FAILED`: check PostgreSQL and `DATABASE_URL`.

Do not use dev mock embeddings, do not fall back to `face_observations`, and do not commit test photos.

