# Phase 3F0.2 — Bbox/Snapshot Alignment Diagnosis Report

Date: 2026-05-25

## Selected Events

### Primary: `55fa26da-f1b3-4396-8501-38c8a224f389`

| Field | Value |
|---|---|
| event_id | `55fa26da-f1b3-4396-8501-38c8a224f389` |
| source_event_id | `savant_phase2c:cam_01:64:intrusion:1779695025161` |
| event_ts_ms | 1779695030159 |
| bbox | `{x: 279.75, y: 396.98, w: 99.94, h: 200.81}` |
| confidence | 0.81 |
| bbox_source | `savant_detection` |
| pre_seconds / post_seconds | 5 / 5 |
| snapshot_offset_seconds | 5.0 |
| clip_path | `/media/replay-sink-output/replay-event-55fa26da-.../video.mov` |
| snapshot_path | `/media/snapshots/55fa26da-...jpg` |
| annotated_snapshot_path | `/media/snapshots/annotated/55fa26da-...jpg` |
| frame_uuid | (null) |
| keyframe_uuid | (null) |

### Secondary: `eb92d385-312f-47c2-b0f8-7dc53715225c` (low-confidence false positive)

| Field | Value |
|---|---|
| bbox | `{x: 0.0, y: 406.90, w: 51.05, h: 202.71}` |
| confidence | 0.09 |
| bbox_source | `savant_detection` |

## Image Dimensions

| Image | Dimensions |
|---|---|
| Raw snapshot | 1920 x 1080 |
| Annotated snapshot | 1920 x 1080 |
| All diagnostic frames (0-9) | 1920 x 1080 |
| Contact sheet | 3840 x 5400 (10-frame grid) |
| Clip video | 1920 x 1080, 30fps, h264 |

## Clip Metadata

- **Total frames**: 301
- **Frame rate**: 30 fps
- **Duration**: ~10 seconds (= pre_5s + post_5s)
- Frame 0: pts=0ms, frame_num=0
- Frame 75: pts=2500ms, frame_num=75
- **Frame 150: pts=5000ms, frame_num=150** ← snapshot extraction point (pre_seconds=5.0)
- Frame 225: pts=7500ms, frame_num=225
- Frame 300: pts=10000ms, frame_num=300

Snapshot extraction timing confirmed correct: frame 150 at 5000ms = 5.0s offset = pre_seconds.

## Diagnostic Frames & Contact Sheets

### Primary event (`55fa26da`)
```
debug/bbox_alignment/55fa26da-f1b3-4396-8501-38c8a224f389/
  contact_sheet.jpg   (3840x5400, 10-frame grid at 1s intervals)
  frame_0.jpg  ...  frame_9.jpg   (1920x1080 each, 1-second spacing)
```

### Secondary event (`eb92d385`)
```
debug/bbox_alignment/eb92d385-312f-47c2-b0f8-7dc53715225c/
  contact_sheet.jpg
  frame_0.jpg  ...  frame_9.jpg
```

## Key Findings

### 1. Coordinate system: CORRECT

- Savant adapter (`person_pose_adapter.py:67-86`) converts from CENTER (xc, yc) to TOP-LEFT (x, y) correctly: `x = xc - w/2, y = yc - h/2`
- `_draw_bbox()` in `annotated_snapshot.py:39-47` draws `[x, y, x+w, y+h]` — correct for top-left origin format
- Bbox coordinates are in pixel space (1920x1080), and the draw function uses the same pixel space

### 2. Snapshot extraction timing: CORRECT

- Snapshot is extracted at clip offset 5.0s (frame 150 of 301)
- Frame_5 from the diagnostic set visually matches the raw snapshot (diff < 2.0)
- media-worker correctly extracts at `pre_seconds` offset

### 3. Bbox draw on snapshot: VISUALLY INACCURATE (root cause found)

The bbox drawn on the annotated snapshot does not align with the person in the image. Root cause is **time-domain mismatch** between two independent video paths:

```
Path A (Savant / bbox source):
  ffmpeg-source → RTSP → Savant → detection → bbox at event_ts_ms

Path B (Replay / snapshot source):
  source-adapter → ZMQ → replay-service → clip → snapshot at offset 5.0s
```

These two paths read the same looping test video file but are **completely independent**:
- Savant reads from RTSP (ffmpeg-source transcodes testVideo/test.mp4 → RTSP)
- source-adapter reads the file directly and sends via ZMQ to replay-service
- The two loops run asynchronously — they are at different positions in the video at any given moment

### 4. Replay keyframe lookup: UNBOUNDED (critical bug)

`services/clip-worker/app/replay_client.py:58-72`:

```python
from_ns = _ts_ms_to_epoch_ns(int(ts_ms - window_s * 1000))
to_ns = _ts_ms_to_epoch_ns(int(ts_ms + window_s * 1000))
# NOTE: from_ns/to_ns are epoch nanoseconds; Replay DB uses
# pipeline-relative timestamps. Unbounded search (omit from/to)
# until timestamp-domain mapping is established.
from_ns = None    # ← DISCARDED
to_ns = None      # ← DISCARDED
```

The keyframe lookup explicitly discards the computed time window because Savant's epoch-nanosecond timestamps don't map to Replay's pipeline-relative timestamps. The result: Replay picks the **most recent keyframe in its buffer** at the time the job is processed — completely unrelated to event_ts_ms.

### 5. Missing alignment fields

Event payload (`behavior_event_export_probe.py:242-243`):
```python
event.frame_uuid = None        # Always None
event.keyframe_uuid = None     # Always None
```

`frame_uuid` and `keyframe_uuid` are set to `None` and never populated. Additionally:
- `bbox_frame_num` is not stored (though `frame_id` is extracted at line 235)
- `event_frame_num` is not stored in payload.media
- Clip metadata `objects` is always `[]` (Replay bypasses Savant entirely)

## Diagnosis Conclusion

**Primarily TIME-DOMAIN MISMATCH**, with a secondary contributory factor of missing frame-level alignment metadata.

Evidence weighted by confidence:

| Factor | Weight | Evidence |
|---|---|---|
| Dual independent video paths | **HIGH** | Savant reads RTSP; replay reads ZMQ from source-adapter. No synchronization. |
| Unbounded keyframe lookup | **HIGH** | Code explicitly discards timestamp window (`from_ns=None, to_ns=None`) |
| Correct coordinate conversion | **CONFIRMED** | xc→x, yc→y verified; draw format verified |
| Correct snapshot timing | **CONFIRMED** | Frame 150 = 5.0s = pre_seconds; visual match with raw snapshot |
| Missing alignment fields | **MEDIUM** | frame_uuid=None, keyframe_uuid=None prevents verification |
| Replay metadata.objects empty | **CONFIRMED** | Expected — Replay bypasses Savant |

The bbox is drawn at the correct pixel coordinates, but on a frame from a **different loop iteration** of the test video than the one Savant was analyzing. In a static test scene with a moving person, this produces a visually misaligned bbox — the person has moved to a different position while the bbox coordinates reflect the person's position in a different (Savant's) view of the video loop.

## Explicit Disclaimers

1. **当前 bbox 不准不是绘制坐标错误。** 坐标域转换（center→top-left）和绘制函数（`[x, y, x+w, y+h]`）均已验证正确。
2. **主要原因是 Savant 和 Replay 视频源不同步。** Savant 读 RTSP，source-adapter 读文件直接 ZMQ 推 Replay — 两个独立循环，不在同一帧。
3. **Replay `metadata.objects` 为空是当前架构预期行为。** Replay 绕过 Savant 直接输出原始帧，没有检测 metadata。
4. **当前 `from_ns`/`to_ns` 在 `replay_client.py:71-72` 被显式丢弃**，keyframe 查找为 unbounded。原因是 Savant epoch 纳秒时间戳与 Replay pipeline-relative 时间戳域不匹配。
5. **真实 bbox overlay 暂不应作为视觉验收通过。** 在输入拓扑或时间域映射修正之前，bbox overlay 无法准确反映检测位置。
6. **下一步必须修正输入拓扑或时间域映射。** 详见 `docs/phase3f0_3_topology_review.md`。

## Recommended Minimum Fix

1. **Anchor keyframe lookup to event timestamp** (highest priority):
   Establish a timestamp-domain mapping between Savant epoch-ns and Replay pipeline-ns. One approach: store the Replay pipeline timestamp alongside each keyframe in a lookup table, keyed by Savant frame PTS. Until this mapping exists, the bbox will always be drawn on the wrong frame.

2. **Store frame-level alignment fields in event payload** (lower priority, enables verification):
   - Set `frame_uuid` from Savant's `frame_meta` (if available)
   - Store `bbox_frame_num` in `payload.media` alongside `event_ts_ms`
   - This allows post-hoc verification of which frame the bbox came from

3. **Short-term workaround for single-file test setup**:
   Consider feeding both Savant and source-adapter from the same RTSP source so their frame sequences are synchronized, or disable bbox overlay until timestamp mapping is implemented.

## Final Conclusion

本报告是 Phase 3F0.2 的完整诊断输出。核心结论：

1. **本报告不证明坐标系统错误。** 坐标域转换（center→top-left）和绘制函数（`[x, y, x+w, y+h]`）均已通过代码审查和视觉验证确认正确。

2. **本报告证明当前拓扑无法支撑可信视觉 bbox overlay。** 根本原因是 Savant 和 Replay 分别独立读取 test.mp4，产生两个异步循环，bbox 与 snapshot 帧内容不匹配。

3. **在 single-ingestion 拓扑或 timestamp-domain mapping 完成之前，不应把 bbox overlay 作为生产视觉证据。** bbox 可以继续绘制用于调试验证，但不得声称"视觉验收通过"。

4. **当前 bbox overlay 只能用于调试验证，不应出现在面向用户的报警界面中作为可信标注。**

5. **下一步必须修正输入拓扑或时间域映射。** 详见：
   - `docs/phase3f0_3_topology_review.md` — 四种方案对比
   - `docs/production_ingestion_topology_policy.md` — 生产拓扑策略
   - `CLAUDE.md` Section 9 — 硬约束规则

## Git Status

```
?? debug/                    ← diagnostic output (NOT committed)
?? manual-inspection/        ← auto-copied images (NOT committed)
?? modules/savant_phase1d/   ← unrelated in-progress module
?? redis-cli                 ← binary
?? temp/                     ← temp files
?? testVideo/test_output_pose.mp4
?? yolo26n-pose.pt           ← model file
?? yolomodel/                ← model directory
```

No modifications to committed files. All diagnosis was read-only analysis plus new diagnostic scripts/files in `debug/` and `docs/phase3f0_2_diagnosis_report.md`.
