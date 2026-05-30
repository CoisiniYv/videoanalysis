# V1.4 Actual Frame Image Source Proof

## Current State

- V1 renderer plumbing: DONE
- V1.1 annotation correctness: NOT ACCEPTED
- V1.2 bbox/source diagnosis: bbox format likely OK, image source untrusted
- V1.3 frame alignment claim: NOT ACCEPTED
- V1.4 required: actual frame image source proof

## Acceptance Boundary

`raw_snapshot.jpg` is only accepted as the actual `record.frame_uuid` image when
the image path is independently resolved from one of these proof sources:

- `runtime_frame_dump_sidecar`
- `frame_uuid_trace_exact_hit`
- `explicit_frame_uuid_index`

Copying `record.frame_uuid` into image metadata, command-line metadata, or a
report field is not proof. A source frame, source clip first frame, latest frame,
or caller-provided frame UUID is untrusted unless one of the proof sources above
resolved the image path.

When proof is not trusted:

- `frame_alignment_status=blocked`
- `visual_correctness_status=blocked`
- no behavior bbox is accepted
- no face bbox or landmarks are accepted
- `face_observation` remains observation metadata only and is not gallery
  recognition

If behavior and face records have different `record_frame_uuid` values but the
same image SHA-256, the resolver status is failed with
`different_records_reused_same_image`.

## Source Extraction

Source video extraction is allowed only after an exact frame UUID trace hit. The
diagnosis must record:

- source video path
- extraction method: `frame_index`, `pts`, or `msec`
- requested and actual frame index when available
- requested and actual PTS milliseconds when available
- video frame count and FPS
- extracted image SHA-256
- whether the extracted frame is the first frame

If `actual_frame_index` is missing or `0`, V1.4 blocks the visual sample with
`source_extraction_returned_first_frame_or_unknown`.

## Runtime Frame Dump

Target debug-only output:

- `/data/video-analytics/media/debug/runtime_frame_dump/{source_id}/{frame_uuid}.jpg`
- `/data/video-analytics/media/debug/runtime_frame_dump/{source_id}/{frame_uuid}.json`

Required sidecar fields:

```json
{
  "source_id": "...",
  "camera_id": "...",
  "frame_uuid": "...",
  "frame_num": 0,
  "frame_pts": 0,
  "image_width": 1920,
  "image_height": 1080,
  "dump_source": "savant_runtime_frame",
  "created_by": "debug_only_runtime_frame_dump"
}
```

Current status:

```text
runtime_frame_dump_status = unavailable
reason = current Savant PyFunc code exposes frame metadata, but this repository
has no safe debug-only runtime image surface extraction helper
```

No unsafe runtime frame dump was added. Any future implementation must be gated
by `VISUAL_DEBUG_FRAME_DUMP=true`, must not access DB, must not start Replay,
must not use media-worker, and must not commit debug JPG/MP4 output.

## Manual Inspection Semantics

The V1.4 manual page reports:

- behavior frame proof: matched / blocked / failed
- face frame proof: matched / blocked / failed
- image proof source
- image SHA-256
- first-frame status
- blocking reason when boxes are not accepted

`visual pass` is not reported unless the image is independently proven to be the
record frame.
