# Model Assets Manifest

This file records required model assets for the video-analytics project.
Models are NOT committed to git.  Users must prepare them manually.

## Face Intelligence Pipeline

### SCRFD_2.5G

- Purpose: Face detection on person/head ROIs
- Expected path: `/data/video-analytics/models/scrfd_2.5g.onnx`
- Input shape: TODO — confirm from ONNX model (typical: 640x640x3)
- Output shape: TODO — confirm from ONNX model
  (typical: scores [1,N,1], bboxes [1,N,4], landmarks [1,N,10])
- Source: TODO — user to confirm official model source/license
- License: TODO — user to confirm
- Phase required: F1

### ArcFace (ResNet / MobileFaceNet variant)

- Purpose: Face embedding extraction (112x112 aligned face crop input)
- Expected path: `/data/video-analytics/models/arcface.onnx`
- Input shape: TODO — confirm from ONNX model (typical: 1x3x112x112)
- Output shape: TODO — confirm from ONNX model
  (typical: 1x512 embedding vector)
- Source: TODO — user to confirm official model source/license
- License: TODO — user to confirm
- Phase required: F1+ (after SCRFD integration)

## Existing Models (already prepared)

### YOLO26-pose

- Path: managed in module config
- Status: integrated in Phase 1/2

## Rules

- Do NOT commit `.onnx`, `.engine`, `.pt`, `.pth` files to git.
- Do NOT commit model weights to git.
- Use `.gitignore` rules for model file extensions.
