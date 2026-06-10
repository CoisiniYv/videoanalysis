# C1F.4b Evidence Overlay Preview

## Goal

C1F.4b generates a static `preview.html` for a C1F.4a evidence bundle. It is
preview only, not the production frontend, and not an API.

The preview proves this frontend rendering contract:

```text
raw_clip.mov + annotations.jsonl -> video element + canvas overlay
```

It does not generate an annotated clip, re-encode video, pull a second RTSP
stream, or use source extraction.

## Input Bundle

The generator expects an evidence bundle with:

- `raw_clip.mov`
- `metadata.json`
- `sink_metadata.json`
- `annotations.jsonl`
- `summary.json`

Example:

```bash
python scripts/tools/generate_evidence_overlay_preview.py \
  --bundle-dir /data/video-analytics/media/evidence/2a22dc8a-362e-4d45-a882-73d0ad6cc82a
```

Output:

```text
/data/video-analytics/media/evidence/{event_id}/preview.html
```

## Overlay Source

`event_annotation.json` is not the main overlay source for C1F.4b. It is the
older single event-frame format and may be partial. For the C1F.4a bundle it has
no overlays.

`annotations.jsonl` is the main overlay source. It is a continuous timeline with
frame-level face objects, bbox, landmarks, identity, pose/action placeholders,
and style fields.

## Time Alignment

The preview must align overlays to raw clip playback by frame PTS:

```text
overlay_time_sec = (annotation.frame_pts - first_video_frame_pts) / 1e9
```

The first video frame PTS comes from the first valid frame record in
`sink_metadata.json`. This matters because Replay raw clips can start from a
keyframe pre-roll before the logical clip window in `summary.json`.

For the C1F.4a bundle:

- First raw video frame PTS: `902184066666`
- Logical `clip_start_ts_ms`: `903649`
- Difference: about `1.465` seconds

Using only `annotation.time_offset_ms / 1000` would shift overlays because it is
relative to the logical event window, not necessarily the first encoded frame in
`raw_clip.mov`. The HTML keeps `time_offset_ms` only as a fallback when
`frame_pts` is missing, and reports that fallback state in the debug panel.

## Preview Behavior

`preview.html` is a single self-contained page with inline CSS and JavaScript.
It fetches bundle-local files only:

- `./metadata.json`
- `./sink_metadata.json`
- `./annotations.jsonl`
- `./summary.json`

The video element uses:

```html
<video src="./raw_clip.mov" controls></video>
```

The canvas overlay scales source coordinates from the raw video size, for
example `1920x1080`, into the displayed video size.

Supported overlay features:

- `bbox.format = cxcywh`
- landmarks
- label text
- similarity
- track id
- timestamp
- `style.bbox_color`

Checkbox controls:

- show unknown faces
- show matched faces
- show landmarks
- show labels

The debug panel displays:

- current video time
- current target PTS
- matched annotation PTS
- active object count
- event id
- source id
- clip status
- decode warning count

## Decode Warnings

If `metadata.json` reports `clip_status = generated_corrupt` or
`clip_validation.decode_error_count > 0`, the preview shows a warning. This does
not block preview generation. The goal is overlay validation, not media repair.

## Boundaries

C1F.4b does not validate or implement:

- production frontend
- API
- watchlist/live_search UI
- annotated clip generation
- video re-encoding
- second RTSP
- source extraction

The production direction remains raw media plus continuous annotation JSON with
frontend overlay rendering.
