# C1F.4c Unified File-Based Evidence Viewer

## Goal

C1F.4c adds a standalone `evidence-viewer` Docker service for browsing evidence
bundles that already exist under `/data/video-analytics/media/evidence`.

The service is file-based. It reads `raw_clip.*`, `metadata.json`,
`summary.json`, `sink_metadata.json`, and `annotations.jsonl` from bundle
directories and renders the overlay in the browser with a video element plus a
canvas. It does not generate evidence and does not change the C1F.4a production
evidence pipeline.

## Non-Goals

- This is not a production frontend.
- It does not implement a formal alarm dashboard.
- It does not implement authentication or authorization.
- It does not add watchlist or live_search features.
- It does not depend on PostgreSQL.
- It does not depend on Redis.
- It does not call the existing FastAPI business API.
- It does not perform source extraction or manual clip creation.
- It does not generate an annotated clip.

## Service Structure

```text
services/evidence-viewer/
  Dockerfile
  requirements.txt
  app/
    __init__.py
    main.py
    config.py
    evidence_index.py
    static/
      index.html
      app.js
      style.css
```

The backend is FastAPI plus Uvicorn. The frontend is a static HTML/CSS/JS page
served by the viewer service.

## Configuration

Environment variables:

```text
EVIDENCE_ROOT=/evidence
EVIDENCE_VIEWER_HOST=0.0.0.0
EVIDENCE_VIEWER_PORT=8090
EVIDENCE_VIEWER_MAX_BUNDLES=200
```

The compose service mounts the host evidence directory as read-only:

```text
/data/video-analytics/media/evidence:/evidence:ro
```

The viewer must not write into the evidence root. Runtime artifacts are not
created under the repository.

## Docker Compose

`infra/docker-compose.c1-official-replay-dev.yml` includes:

```text
service: evidence-viewer
container: c1-official-evidence-viewer
port: 8090:8090
mount: /data/video-analytics/media/evidence:/evidence:ro
```

The service has no GPU, no privileged mode, no PostgreSQL dependency, and no
Redis dependency.

## API

`GET /health`

Returns service status and confirms read-only mode:

```json
{
  "status": "ok",
  "evidence_root": "/evidence",
  "read_only": true
}
```

If the evidence root is missing the service returns degraded health instead of
crashing.

`GET /api/bundles`

Scans the first directory level below `EVIDENCE_ROOT` and returns bundle
summaries. Query filters:

```text
event_type
source_id
camera_id
event_id
person
clip_status
limit
offset
```

`GET /api/bundles/{event_id}`

Returns the bundle manifest with parsed `metadata.json`, parsed `summary.json`,
available files, warnings, and URLs for media and overlay data.

`GET /api/bundles/{event_id}/annotations`

Returns parsed `annotations.jsonl` records and parse warnings. Empty lines and
bad lines are tolerated. `?format=jsonl` returns the original JSONL.

`GET /api/bundles/{event_id}/sink-metadata`

Returns parsed sink metadata records. Both JSON array and JSONL input are
supported.

`GET /api/bundles/{event_id}/media/raw_clip`

Serves the raw clip with `FileResponse`. The viewer resolves `raw_clip.*`, not a
hard-coded `.mov` path. Preferred order:

```text
raw_clip.mp4
raw_clip.mov
raw_clip.webm
raw_clip.mkv
```

If `metadata.json` contains a raw clip path or name, the backend may use it only
after verifying that it resolves inside the event bundle.

## Path Traversal Defense

`event_id` is treated as a single evidence-root child directory name. The
backend rejects:

```text
../
..%2F
absolute paths
nested path segments
unsafe characters
symlink escapes
```

Implementation rules:

- `event_id` must match `[A-Za-z0-9_.:-]+`.
- The resolved bundle path must remain under the resolved evidence root.
- The raw clip file path must resolve under the resolved bundle directory.

## Overlay Data Source

`annotations.jsonl` is the main overlay data source. It is a continuous
timeline generated in C1F.4a.

`event_annotation.json` is an older single-frame or event-level partial format.
It is not the primary overlay source for this viewer.

The browser loads:

```text
metadata.json
summary.json
sink_metadata.json
annotations.jsonl via /api/bundles/{event_id}/annotations
raw_clip.* via /api/bundles/{event_id}/media/raw_clip
```

## Time Alignment

Raw clips can start earlier than the logical annotation window because Replay
clips anchor on keyframes and may include pre-roll. Therefore overlay alignment
must prefer video frame PTS:

```text
overlay_time_sec = (annotation.frame_pts - first_video_frame_pts) / 1e9
```

The viewer finds `first_video_frame_pts` from the first sink metadata record
with `pts`. If an annotation line has no `frame_pts`, the viewer falls back to:

```text
annotation.time_offset_ms / 1000
```

The fallback is recorded as `time_offset_ms_fallback` in the warning panel.

## Bbox Formats

The overlay normalizes these formats into clamped pixel `xyxy` rectangles:

```text
cxcywh
xyxy
xywh
```

The source dimensions come from sink metadata or from the loaded video. They are
not hard-coded to 1920x1080.

## Unknown and Matched Display Semantics

Matched faces show a name or external person id plus real similarity:

```text
Reese 0.500
```

Unknown faces show neutral labels:

```text
Unknown face
```

Unknown faces must not display `sim 0.00`, `0.sim`, or `score 0.00` unless a
future annotation explicitly carries a real similarity with non-unknown
semantics.

The viewer preserves the style policy structure (`reason`, `priority`, color)
but does not apply event-level alert red to every object. If an unknown object
arrives with alert red from an event-level style, the viewer uses a neutral
color and records:

```text
unknown_style_overridden_from_event_alert
```

Matched or alert objects may use `annotation.style.bbox_color`. Low similarity
candidates use a warning color. Unknown faces use neutral color.

## generated_corrupt Semantics

`generated_corrupt` or nonzero decode warnings are not treated as preview
failure. The viewer displays:

```text
Evidence generated, source decode warnings observed
```

Playback and overlay remain enabled.

## Smoke Commands

```bash
pytest harness/tests/test_c1f4c_evidence_viewer_contract.py

bash scripts/smoke/check_c1f4c_evidence_viewer.sh

docker compose -f infra/docker-compose.c1-official-replay-dev.yml up -d evidence-viewer
curl -fsS http://localhost:8090/health
curl -fsS http://localhost:8090/api/bundles
```

The smoke returns `PASS_C1F4C_UNIFIED_FILE_BASED_EVIDENCE_VIEWER` when a real
bundle is available and verified. If no evidence bundle exists but service and
contract checks pass, it returns `PASS_CONTRACT_ONLY_REAL_BUNDLE_NOT_VERIFIED`.

## Known Limits

- The viewer is a dev evidence inspection tool, not a production frontend.
- It is read-only and does not edit or annotate bundles.
- It does not query the database for extra identity or event context.
- It does not produce positive recognition events.
- It does not repair raw media with decode warnings.
