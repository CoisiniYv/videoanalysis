# C1M Evidence Alignment Root Cause

Status: consolidated C1M summary.

C1M established that raw clip extraction was not the first failure layer. The
raw clip decoded consistently by time, index, and sink metadata, while legacy
and sidecar annotation rows could bind to a different visual stream epoch when
the current looped RTSP source reused `frame_pts` / `frame_num`.

## Findings

- Replay raw clip extraction was correct for the controlled evidence.
- `by_time`, `by_index`, and `by_sink_meta` selected the same decoded raw frame.
- Legacy annotations and selected sidecar rows did not match the trigger frame
  or nearby frames when matching fell back to non-unique PTS/frame numbers.
- The looped RTSP source can reset or repeat `frame_pts` / `frame_num`.
- Replay sink metadata does not currently provide enough epoch/session identity
  to prove rows came from the same visual stream instance.
- Therefore `source_id + frame_pts/frame_num` is insufficient production frame
  identity for looped RTSP.

## Runtime Guard

C1M.8 added a freshness guard for frame-cache rows selected through PTS fallback.
Rows matched by frame UUID remain preferred. Rows matched by PTS fallback must
have `frame_annotation.created_at` within the event request window. Stale or
ambiguous rows are rejected and counted as:

- `rows_rejected_stale_cache`
- `rows_rejected_epoch_mismatch`
- `rows_rejected_pts_non_unique`

If stale rows affect production output, the sidecar reports:

- `annotation_status=cache_stale_or_epoch_mismatch`
- `production_ready=false`
- no legacy/SQL fallback for watchlist production

## Watchlist Visual Binding

C1M.9 added explicit visual binding fields so a `watchlist_hit` event can exist
without being presented as visually confirmed:

- `visual_binding_status`
- `visual_binding_reason`
- `evidence_visual_status`
- `source_observation_id`
- `frame_identity_method`
- `frame_identity_confidence`

Viewer auto selection must show unavailable when production sidecar is not
ready for a watchlist bundle. Manual legacy/debug annotations may be inspected,
but known-face visual confirmation is stripped and marked unverified.

## Remaining Requirement

The freshness guard is a fail-closed safety guard, not a complete identity
model. Robust production binding still requires propagating one of:

- `source_epoch_id`
- `source_session_id`
- `adapter_session_id`
- `replay_stream_id`
- exact Replay/source-session `frame_uuid` / keyframe UUID binding
