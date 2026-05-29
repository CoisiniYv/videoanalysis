# R2 Pipeline Runtime Verification

Date: 2026-05-29
Status: R2.2 static verified and runtime verified for module/model initialization.

## Result

Static review confirms that the c1-official-adapter runtime is wired to the main `modules/savant_security` pipeline and that this module contains the first locked dual-primary architecture:

```text
frame -> YOLO26-pose -> person bbox / keypoints / track_id
frame -> YOLOv8-Face full-frame primary -> face bbox / 5 landmarks
person + face -> face-person association
face -> AdaFace -> 512-d embedding
embedding -> face_observation_exporter -> Redis security.face_observations
```

Here "dual-primary" means both pose and full-frame face branches exist in the logical Savant pipeline. It does not claim GPU kernel-level physical concurrency. On a single GPU the TensorRT engines are scheduled by the runtime through time-slicing and pipeline parallelism.

This means the F4.3 smoke is not configured as a face-only module by static configuration review.

It also means YOLOv8-Face remains a full-frame primary detector. It is not configured as a per-person crop secondary detector under YOLO26-pose.

## Static Evidence

`infra/docker-compose.c1-official-adapter.yml`:

- Service `savant-security` uses working directory `/opt/savant/src/module`.
- It mounts `../modules/savant_security:/opt/savant/src/module:rw`.
- It starts `python -m savant.entrypoint module.yml`.
- It sets `ZMQ_SRC_ENDPOINT=router+bind:tcp://0.0.0.0:5555`.

`modules/savant_security/module.yml` contains:

- `name: yolo26_pose`
- `name: tracker` with `element: nvtracker`
- `name: behavior_rules`
- `name: yolov8_face` as a full-frame detector with no `input.object` or `input.objects` dependency on `yolo26_pose.person`
- `name: face_person_associator`
- `name: adaface`
- `name: face_observation_exporter`

`infra/docker-compose.c1-official-adapter.yml` sets:

- `FACE_OBSERVATION_STREAM: "security.face_observations"`
- `../modules/savant_security:/opt/savant/src/module:rw`

The face observation exporter sends metadata and embeddings to Redis. It does not send image bytes.

## Architecture Boundary

Do not change the first mainline version to:

```text
YOLO26-pose -> person crops -> YOLOv8-Face per crop
```

The default remains:

```text
YOLOv8-Face full-frame primary
```

Reasons:

1. For the 60-stream dual-T4 target, full-frame face detector cost is fixed and capacity is easier to reason about.
2. Per-person crop secondary detection scales inference work with people count, making crowded-scene worst case harder to bound.
3. The current pipeline already has face-person association, so crop-based natural association is not required for the first version.
4. Small-face recall can be evaluated later, but should not be solved by changing the mainline architecture during R2.

Person-crop face detection is only a future option. Any switch to a cascade must go through small-face recall tests and crowded-scene worst-case performance tests.

## Runtime Status

- Current verification level: runtime verified for module graph and model initialization.
- R2.2 fresh local mp4 run used source id `r2_dual_primary_verify_1780032064` and adapter location `/testVideo/1080movie.mp4`.
- The same `c1-official-savant` run logged the logical pipeline:

```text
zeromq_source_bin(source) -> nvstreammux(muxer) -> nvinfer(yolo26_pose) -> nvtracker(tracker) -> pyfunc(behavior_rules) -> nvinfer(yolov8_face) -> pyfunc(face_person_associator) -> nvinfer(adaface) -> pyfunc(face_reid_gate) -> pyfunc(face_observation_exporter) -> pyfunc(face_embedding_debug) -> pyfunc(face_debug) -> nvstreamdemux(demuxer)
```

Runtime logs showed TensorRT engine load success for:

- `yolo26_pose`: `/models/yolo26_pose/yolo26_pose.onnx_b1_gpu0_fp16.engine`
- `yolov8_face`: `/models/yolov8_face/yolov8n-face.onnx_b1_gpu0_fp16.engine`
- `adaface`: `/models/adaface/adaface_ir50_webface4m.onnx_b16_gpu0_fp16.engine`

Runtime logs also showed:

- `gstnvtracker: Loading low-level lib ... libnvds_nvmultiobjecttracker.so`
- `[NvMultiObjectTracker] Initialized`
- `stage=savant_security_face_obs_exporter_init ... stream=security.face_observations`

PostgreSQL received a valid face observation for the R2.2 source id:

```text
source_observation_id=face:r2_dual_primary_verify_1780032064:3:8049
camera_id=cam_r2_dual_primary_verify
source_id=r2_dual_primary_verify_1780032064
track_id=3
timestamp_ms=8049
embedding_model=adaface
embedding_dim=512
embedding_norm=0.999905
```

This verifies the face branch and Redis/DB observation path at runtime. It also verifies the pose branch and tracker are present and initialized in the same runtime pipeline. It does not by itself validate pose keypoint accuracy or crowded-scene tracking quality.

Suggested readonly checks:

```bash
docker logs c1-official-savant --tail 300 | grep -E 'yolo26_pose|yolov8_face|adaface|face_observation_exporter|behavior_rules|nvtracker'
docker inspect c1-official-savant --format '{{range .Mounts}}{{println .Source "->" .Destination}}{{end}}'
```

## Limits

This document does not modify the Savant pipeline or Redis producer. It only records the current wiring before R2 performance preparation.
