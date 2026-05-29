# Operator: Add an Algorithm

Status: R2 operator draft.

## Purpose

Add new analytics behavior without destabilizing the main Savant runtime. Keep domain logic testable before wiring it into Savant.

## Principles

1. Build behavior-rule algorithms first in pure Python harness tests.
2. Use Savant PyFunc only as the runtime adapter.
3. Keep model assets explicit in `docs/model_assets_manifest.md`.
4. Add a contract test for configuration and module wiring.
5. Add a smoke test before calling the algorithm ready.

## Current Algorithm Stack

- YOLO26-pose detects persons and keypoints.
- `nvtracker` assigns person track ids.
- `behavior_rules` runs ROI/rule logic.
- YOLOv8-Face is the current full-frame primary face detector.
- AdaFace is the current face embedding model.
- `face_observation_exporter` emits face observations to Redis.

Do not casually switch YOLOv8-Face to SCRFD or AdaFace to ArcFace. A model change requires asset verification, preprocessing review, updated contract tests, and a smoke run.

## Recommended Flow

1. Define the event schema or observation schema.
2. Implement pure Python logic under `modules/savant_security/custom/rules` or a service module.
3. Add harness tests for edge cases and timing behavior.
4. Add PyFunc adapter code only after the pure logic is stable.
5. Add configuration fields to the camera/rule config loader.
6. Update `docs/model_assets_manifest.md` if a model is involved.
7. Add a smoke script under `scripts/smoke`.
8. Document common troubleshooting and expected Redis/DB outputs.

## Common Troubleshooting

- Algorithm works in harness but not Savant: inspect object labels, attribute names, and source metadata.
- No tracks: verify YOLO26-pose and `nvtracker` are running.
- No face embeddings: verify YOLOv8-Face detections, AdaFace input object, and min face size.
- Wrong camera rules: verify `CAMERAS_CONFIG_PATH` and generated camera config.

Performance testing belongs after R2 consolidation, not during algorithm bring-up.

