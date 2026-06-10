# Phase E1 Run Report — 2026-05-25 10:51 UTC

## Run Summary

- **Pipeline started**: 10:48 UTC
- **Pipeline stopped**: 10:50 UTC (~90s runtime)
- **Evidence-worker run**: 10:51 UTC
- **Events generated**: 481 intrusion events
- **Evidence processed**: 2/5 events (3 skipped — track_id not in frozen metadata)
- **Commit**: `7d19e21`

## Generated Evidence

### Event 1: `c86abb18-1e4b-407a-8fcd-dbdb95f27bff`

| Field | Value |
|---|---|
| event_type | intrusion |
| track_id | 347 |
| source_id | phase3h |
| selected frame_num | 3078 (from 95 matching frames) |
| objects in frame | 13 person bboxes |
| snapshot_path | `/media/evidence/snapshots/c86abb18-1e4b-407a-8fcd-dbdb95f27bff.jpg` |
| annotated_snapshot_path | `/media/evidence/snapshots/annotated/c86abb18-1e4b-407a-8fcd-dbdb95f27bff.jpg` |
| clip_path | `/media/evidence/clips/c86abb18-1e4b-407a-8fcd-dbdb95f27bff.mp4` |
| snapshot_url | `http://localhost:8002/media/evidence/snapshots/c86abb18-1e4b-407a-8fcd-dbdb95f27bff.jpg` |
| annotated_snapshot_url | `http://localhost:8002/media/evidence/snapshots/annotated/c86abb18-1e4b-407a-8fcd-dbdb95f27bff.jpg` |
| clip_url | `http://localhost:8002/media/evidence/clips/c86abb18-1e4b-407a-8fcd-dbdb95f27bff.mp4` |

Snapshot: 1920x1080 RGB JPEG, 185KB  
Annotated: 1920x1080 RGB JPEG, 269KB — 9,869 red bbox pixels, 16,395 dark label pixels, 662,725 pixels modified  
Clip: 1.5MB, ~3.2s (keyframe-aligned, see limitations)

### Event 2: `bcb25734-6ac6-4314-b441-92f101adf8e0`

| Field | Value |
|---|---|
| event_type | intrusion |
| track_id | 337 |
| source_id | phase3h |
| selected frame_num | 3118 (from 131 matching frames) |
| objects in frame | 11 person bboxes |
| snapshot_path | `/media/evidence/snapshots/bcb25734-6ac6-4314-b441-92f101adf8e0.jpg` |
| annotated_snapshot_path | `/media/evidence/snapshots/annotated/bcb25734-6ac6-4314-b441-92f101adf8e0.jpg` |
| clip_path | `/media/evidence/clips/bcb25734-6ac6-4314-b441-92f101adf8e0.mp4` |
| snapshot_url | `http://localhost:8002/media/evidence/snapshots/bcb25734-6ac6-4314-b441-92f101adf8e0.jpg` |
| annotated_snapshot_url | `http://localhost:8002/media/evidence/snapshots/annotated/bcb25734-6ac6-4314-b441-92f101adf8e0.jpg` |
| clip_url | `http://localhost:8002/media/evidence/clips/bcb25734-6ac6-4314-b441-92f101adf8e0.mp4` |

Snapshot: 1920x1080 RGB JPEG, 187KB  
Annotated: 1920x1080 RGB JPEG, 268KB — 11 bboxes drawn  
Clip: 1.9MB

## Database State

Both events have full evidence records in PostgreSQL:

| Column | Event c86abb18 | Event bcb25734 |
|---|---|---|
| snapshot_path | set | set |
| clip_path | set | set |
| payload.media.annotated_snapshot_path | set | set |
| payload.media.snapshot_status | ready | ready |
| payload.media.clip_status | ready | ready |
| payload.media.annotated_snapshot_status | ready | ready |
| payload.media.evidence_frame_num | 3078 | 3118 |
| payload.media.evidence_track_id | 347 | 337 |

## API Verification

- API running on port **8002** (not 8001 — port conflict with existing service)
- All 3 URL types returned HTTP **200**
- `annotated_snapshot_url` resolved from `payload.media.annotated_snapshot_path` via existing fallback — **no API code changes needed**

## Annotated Snapshot Content

Event c86abb18 annotated snapshot:
- **13 red bbox rectangles** visible (9,869 red pixels)
- **Label block** in top-left corner (16,395 dark pixels) showing event ID, type, track_id, frame number
- **662,725 pixels** differ from raw snapshot → substantial overlay content

## Skipped Events

3 events (track_ids 390, 389, 378) skipped because those track_ids were not found in metadata.json. This is expected: these are newer tracks assigned after the ~3028 frames captured in the frozen metadata file. The pipeline was stopped, so newer tracks never had frames written to metadata.json.

## Known Issues Found During Run

### 1. Clip duration shorter than expected (~3.2s vs 6.0s requested)
- **Cause**: ffmpeg `-c copy` with keyframe-aligned input seeking. Keyframes every 25 frames (0.83s), so the actual clip window is bounded by keyframe positions.
- **Impact**: Clip may be shorter or longer than the requested pre-3s + post-3s window.
- **Fix for future**: Use `-ss` after `-i` (decode seeking) for precise start, or re-encode the clip instead of `-c copy`.

### 2. `EVIDENCE_MAX_EVENTS=5` but some tracks not in metadata
- Events are ordered by `created_at DESC` (most recent first), but frozen metadata may not contain recent tracks.
- **Impact**: Some events can't be processed in a single evidence-worker run.
- **Mitigation**: Run evidence-worker again after re-running the pipeline to capture more frames.

### 3. evidence-worker outputs are owned by root
- Container runs as root, files written to host volume are root-owned.
- **Impact**: Host-level cleanup requires sudo/root.

## Files on Disk

```
media/evidence/
  snapshots/
    c86abb18-1e4b-407a-8fcd-dbdb95f27bff.jpg       185K
    bcb25734-6ac6-4314-b441-92f101adf8e0.jpg       187K
    annotated/
      c86abb18-1e4b-407a-8fcd-dbdb95f27bff.jpg     269K
      bcb25734-6ac6-4314-b441-92f101adf8e0.jpg     268K
  clips/
    c86abb18-1e4b-407a-8fcd-dbdb95f27bff.mp4       1.5M
    bcb25734-6ac6-4314-b441-92f101adf8e0.mp4       1.9M
```

---

*Generated 2026-05-25. Phase E1 — Alert Evidence MVP (Single-Ingestion Dev Path).*
