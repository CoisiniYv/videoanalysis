# C1F.1a Dual Primary Current State Audit

Date: 2026-06-01

Status: PASS_CONFIG_READY

## Scope

Read-only audit of whether the C1 official replay dev stack is configured for
same-frame YOLO26-pose + YOLOv8-Face dual primary detection. No runtime
changes, no new business features, no watchlist/live_search/API/frontend.

## Audit Results

### Pipeline Configuration (module.yml)

| Check | Result | Evidence |
|-------|--------|----------|
| YOLO26-pose full-frame primary | ✅ | `yolo26_pose` — first nvinfer, no `input.object`, label=person |
| YOLOv8-Face present | ✅ | `yolov8_face` — second nvinfer, `YoloV8faceConverter`, label=face |
| YOLOv8-Face full-frame primary | ✅ | No `input.object` binding, receives full frame |
| YOLOv8-Face person ROI secondary | ❌ NOT | No ROI parent configured |
| nvtracker person-only | ✅ | Tracker sits after yolo26_pose, before yolov8_face; only person objects tracked |

### Face-Person Association

| Check | Result | Evidence |
|-------|--------|----------|
| Association exists | ✅ | `face_person_associator` pyfunc in pipeline |
| Located after pose + face detection | ✅ | Pipeline order: yolo26_pose → nvtracker → behavior_rules → yolov8_face → face_person_associator |
| Geometry-only (no inference) | ✅ | Pure bbox spatial matching in `face_person_association.py`, zero DL imports |
| No image cropping | ✅ | Reads bbox coordinates only |

### Frame Anchor Metadata

| Check | Result | Evidence |
|-------|--------|----------|
| frame_uuid available | ✅ | `video_frame.uuid` via savant_rs |
| frame_num available | ✅ | `frame_meta.frame_num` |
| frame_pts available | ✅ | `video_frame.pts` or `frame_meta.pts` |
| timestamp_ms available | ⚠️ INDIRECT | Not in anchor dict directly; computed via `normalize_pts_to_ms(pts)` in callers |

### Topology (C1E Compose)

| Check | Result | Evidence |
|-------|--------|----------|
| RTSP → Source Adapter → Replay → Savant | ✅ | `source-adapter` → `replay-service:5555` → `savant-security:5557` |
| Fixed RTSP URL | ✅ | `rtsp://10.37.57.112:8554/live/1080movie` |
| Source Adapter RTSP_TRANSPORT=tcp | ✅ | `RTSP_TRANSPORT: ${C1E_RTSP_TRANSPORT:-tcp}` |
| No second RTSP | ✅ | Single source-adapter, single RTSP URI |
| No source extraction fallback | ✅ | Not present in compose |
| No production annotated_clip | ✅ | Not generated |

### Face Pipeline Readiness (existence check, not runtime pass)

| Component | Present | Model Asset |
|-----------|---------|-------------|
| AdaFace element | ✅ `adaface` in module.yml | ✅ `adaface_ir50_webface4m.onnx` + `.engine` |
| face_reid_gate | ✅ pyfunc exists | N/A (pure Python) |
| face_observation_exporter | ✅ pyfunc exists | N/A (pure Python) |
| YOLOv8-Face model | ✅ | ✅ `yolov8n-face.onnx` + `.engine` |
| security.face_observations stream | ✅ configured | Not required for C1F.1a pass |

### Forbidden Path Check

| Check | Result |
|-------|--------|
| No second RTSP | ✅ |
| No source extraction | ✅ |
| No production annotated_clip | ✅ |
| No image bytes in Redis | ✅ (embeddings only, not raw images) |

## Pipeline Element Order

```text
1. yolo26_pose       (nvinfer, full-frame, person detection + keypoints)
2. tracker           (nvtracker, person-only tracking)
3. behavior_rules    (pyfunc, intrusion rule, SecurityEvent export)
4. yolov8_face       (nvinfer, full-frame, face detection + landmarks)
5. face_person_associator  (pyfunc, geometry-only bbox matching)
6. adaface           (nvinfer, face embedding, 512-d)
7. face_reid_gate    (pyfunc, quality gate + per-track throttle)
8. face_observation_exporter  (pyfunc, Redis export)
9. face_embedding_debug       (pyfunc, smoke visibility probe)
```

## Conclusion

**PASS_CONFIG_READY**

The dual primary detection configuration is correct:
- Both YOLO26-pose and YOLOv8-Face operate on the full frame as primary detectors.
- Face is NOT a person ROI secondary detector.
- nvtracker tracks person objects only (face detections are untracked).
- Face-person association is geometry-only, positioned after both detections.
- Frame anchor metadata (frame_uuid, frame_num, frame_pts) is available for same-frame alignment.
- All model assets (YOLOv8-Face ONNX+engine, AdaFace ONNX+engine) are present.
- The C1E topology is RTSP → Source Adapter → Replay → Savant, single path, TCP.

Not required for this phase: face detection runtime PASS, AdaFace runtime PASS,
face_observations PASS, watchlist/live_search.
