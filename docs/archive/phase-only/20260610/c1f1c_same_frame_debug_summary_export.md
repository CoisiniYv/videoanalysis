# C1F.1c Same-Frame Pose + Face Debug Summary Export

Date: 2026-06-01

Status: PASS_DEBUG_EXPORT_READY

## Scope

Add a lightweight debug probe to the C1E replay dev stack that exports
same-frame pose + face detection summary as JSONL. This enables offline
verification of dual primary detection without runtime smoke.

## Implementation

New pyfunc: `SameFrameDetectionDebugPyFunc`

File: `modules/savant_security/custom/pyfuncs/same_frame_detection_debug.py`

Pipeline position: after `face_person_associator`, `adaface`, `face_reid_gate`,
`face_observation_exporter`, and `face_embedding_debug`. This ensures all
association and embedding metadata is available when the summary is built.

Environment gate: `C1F1_SAME_FRAME_DEBUG_ENABLED=1` (disabled by default).

## Output

Path: `/data/video-analytics/artifacts/c1f1/same_frame_pose_face_summary.jsonl`

Format: one JSON object per line (JSONL). Each record covers one frame.

## Schema

```json
{
  "source_id": "c1e_rtsp_replay",
  "camera_id": "cam_c1e_rtsp_replay",
  "frame_uuid": "...",
  "frame_num": 123,
  "frame_pts": 123456789,
  "timestamp_ms": 1780247000000,
  "pose": {
    "person_count": 1,
    "persons": [
      {
        "track_id": "6",
        "bbox": [x1, y1, x2, y2],
        "confidence": 0.82,
        "keypoints_count": 17,
        "visible_keypoint_count": 14,
        "mean_keypoint_confidence": 0.73
      }
    ]
  },
  "face": {
    "face_count": 1,
    "faces": [
      {
        "bbox": [x1, y1, x2, y2],
        "confidence": 0.91,
        "landmarks_count": 5
      }
    ]
  },
  "association": {
    "matched_face_person_pairs": [
      {
        "track_id": "6",
        "association_method": "center_inside_upper_body",
        "association_score": 0.64
      }
    ],
    "unmatched_face_count": 0,
    "reason_if_no_match": null
  }
}
```

### Null/empty handling

- No persons in frame: `pose.person_count=0`, `pose.persons=[]`,
  `reason_if_no_match="no_persons_detected"`.
- No faces in frame: `face.face_count=0`, `face.faces=[]`,
  `reason_if_no_match="no_faces_detected"`.
- No persons AND no faces: `reason_if_no_match="no_persons_and_no_faces"`.
- Persons exist but no geometric match: `reason_if_no_match="no_geometric_match_found"`.

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `C1F1_SAME_FRAME_DEBUG_ENABLED` | (unset) | Must be `1`/`true`/`yes` to enable |
| `C1F1_SAME_FRAME_OUTPUT_DIR` | `/data/video-analytics/artifacts/c1f1` | Output directory |
| `C1F1_SAME_FRAME_OUTPUT_FILE` | `same_frame_pose_face_summary.jsonl` | Output filename |
| `C1F1_SAME_FRAME_MAX_FRAMES` | `0` | Max frames to write (0=unlimited) |

## Boundaries

- No full frame images output.
- No face crop output.
- No JPEG/PNG/RAW output.
- No image bytes in Redis.
- No annotated_clip generation.
- No watchlist_hit / live_search_hit.
- No second RTSP.
- No source extraction fallback.
- Does NOT change business event semantics.
- Read-only probe: inspects metadata, writes summary, nothing else.
