# Midterm UUID Frame Identity Correction Report

Date: 2026-07-08

## Summary

This correction standardizes evidence anchoring around Savant frame identity instead
of mixing camera/display timestamps with frame lookup fields.

The corrected contract is:

- `frame_uuid` is the primary identity for an analyzed frame and for image
  evidence lookup.
- `keyframe_uuid` and `previous_keyframe_uuid` are video/replay window anchors.
- `frame_pts` is a frame-domain time used for ordering, window arithmetic, and
  exact or nearest fallback only.
- `event_ts_ms` for frame-origin face events is derived from `frame_pts`, so
  cooldown, event windows, and evidence lookup stay in the same frame domain.
- `ntp_timestamp` remains metadata for camera/display/audit time. It is no
  longer used as the watchlist evidence anchor.

## Findings

The latest pressure-run audit showed that retained events carried frame UUIDs,
but watchlist events were using a different time domain for `event_ts_ms`:

- `intrusion`: `event_ts_ms - frame_pts_ms` was approximately 0 ms.
- `watchlist_hit`: `event_ts_ms - frame_pts_ms` had a p50 around 11.8 seconds
  and a max around 15.1 seconds.

That means the watchlist path had frame identity available, but it still used
`ntp_timestamp` as the event time. This made image lookup and annotation-window
selection depend on a camera/display timestamp rather than the actual Savant
frame that produced the face match.

There was a second consistency issue: face observation IDs were still primarily
timestamp-derived. That works as a legacy fallback, but when `frame_uuid` exists
the ID should identify the frame directly.

## Code Changes

### Watchlist event time

Updated `services/face-worker/app/face_match_event_service.py`:

- `watchlist_hit.event_ts_ms`, `start_ts_ms`, and `end_ts_ms` now prefer
  `payload.media.frame_pts // 1_000_000`.
- `ntp_timestamp` is preserved in `payload.media.ntp_timestamp` for audit and
  display, but it is not used for evidence anchoring.
- Watchlist media payload now declares:
  - `frame_identity_anchor = "frame_uuid"`
  - `time_domain = "savant_frame"`

### Face observation ID

Updated `modules/savant_security/custom/models/face_events.py`:

- When `frame_uuid` is available, `source_observation_id` uses:
  `face:{source_id}:uuid:{frame_uuid}:{face_index}`.
- Legacy callers without `frame_uuid` keep the old format:
  `face:{source_id}:{track_id_or_no_track}:{timestamp_ms}`.
- Legacy multi-face disambiguation still appends `:{face_index}` for
  `face_index > 0`.

### Savant exporter and frame annotations

Updated:

- `modules/savant_security/custom/pyfuncs/face_observation_exporter.py`
- `modules/savant_security/custom/services/frame_annotation_builder.py`

Both paths now pass `frame_uuid` into the shared observation-ID builder. That
keeps face observations, watchlist events, and frame annotations aligned on the
same frame identity.

## Tests

Added or updated tests for:

- Watchlist time-domain behavior: frame PTS wins over NTP.
- UUID-based face observation ID format.
- Legacy ID compatibility.
- Stream-session propagation with frame UUID and frame PTS.

Validation run:

```bash
pytest -q \
  harness/tests/test_face_match_evidence_policy.py \
  harness/tests/test_face_observation_event.py \
  harness/tests/test_face_observation_exporter.py \
  harness/tests/test_midterm_stream_session_isolation.py \
  harness/tests/test_evidence_materialization_phase2plus.py \
  harness/tests/test_evidence_viewer_database_index.py \
  harness/tests/test_frame_annotation_event_window.py \
  harness/tests/test_evidence_viewer_frame_identity_static.py
```

Result:

```text
115 passed
```

Compile validation:

```bash
PYTHONPYCACHEPREFIX=/tmp/video-analytics-pycache python -m py_compile \
  services/face-worker/app/face_match_event_service.py \
  modules/savant_security/custom/models/face_events.py \
  modules/savant_security/custom/pyfuncs/face_observation_exporter.py \
  modules/savant_security/custom/services/frame_annotation_builder.py \
  services/media-worker/app/worker.py
```

Result: passed.

Note: running `py_compile` without `PYTHONPYCACHEPREFIX` hit a local
permission-denied error while writing into an existing `__pycache__`; compiling
to `/tmp` avoids touching that cache and validates the same source files.

## Expected Runtime Impact

The next pressure run should no longer produce watchlist evidence misses caused
by `event_ts_ms` drifting away from the frame that generated the face match.
If image evidence still misses after this correction, the remaining causes are
more likely to be real rolling-cache metadata loss, missing frame annotations,
or image-export/materializer throughput rather than UUID/PTS/NTP domain mixing.
