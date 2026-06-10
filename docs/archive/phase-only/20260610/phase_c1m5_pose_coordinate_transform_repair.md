# C1M Pose Keypoint And Coordinate Repair

Status: consolidated C1M pose summary.

C1M separated face and pose overlay evidence, then narrowed the pose failure to
YOLO26-pose coordinate restoration and keypoint propagation. Face/pose converter
logic outside this scope was not modified during the cache/binding phases.

## Keypoint Export

Frame annotations now support configurable pose keypoint export:

- `FRAME_ANNOTATION_INCLUDE_KEYPOINTS=compact` keeps the compact summary.
- `FRAME_ANNOTATION_INCLUDE_KEYPOINTS=full` exports COCO17 points with
  `index`, `name`, `x`, `y`, and `confidence`.
- `FRAME_ANNOTATION_INCLUDE_KEYPOINTS=debug` uses the same full point shape for
  diagnostics.

Production sidecar rows preserve `pose` for person rows, allowing offline or
viewer renderers to use the same sidecar source rather than legacy annotations.

## Coordinate Restore

The YOLO26-pose model input is `640x640`; production frames are commonly
`1920x1080`. For letterboxed input the expected transform is:

- scale: `1/3`
- `pad_x`: `0`
- `pad_y`: `140`

The converter now has `coordinate_restore_mode`, defaulting to `letterbox`.
The same restore transform is applied to person bbox and COCO17 keypoints.
`stretch` remains available for compatibility with non-letterboxed inputs.

The repair preserves the existing confidence filtering, class filtering, and
NMS path. The retained regression tests cover:

- letterbox scale/padding computation
- bbox restore with y unpadding before scaling
- keypoint restore with the same transform
- legacy stretch math
- converter output and optional debug JSONL records
