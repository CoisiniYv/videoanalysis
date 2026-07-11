# Evidence bbox long-run annotation cache fix (2026-07-11)

## Symptom

During the production T4 60-stream, 4 FPS, two-hour run
`pressure60_4p1_dual1gpu_retain2h_20260711T034003Z`, 184 video bundles were
playable and had DB timelines, but only 101 had non-empty DB overlay rows. The
remaining 83 bundles displayed video without bbox overlays on port 8090.

## Root cause

The media-worker correctly detected that the per-source frame-annotation stream
was behind the event anchor by roughly 10 to 21 seconds and retried. However,
the production pressure profile enabled a 900-second frame-annotation range
cache. Every retry reused the same cached, incomplete Redis range. In addition,
the Redis stream-ID upper bound was derived from event time plus the evidence
window. When exporter delay exceeded that window, a late annotation had a
newer Redis entry ID and remained outside the bounded read even though its
payload `frame_pts` belonged to the clip. After the 30-second wait limit,
media-worker accepted `missing_frame_metadata` with zero annotation rows,
indexed that empty result, and pruned the successful bundle's sidecars.

This was not an 8090 canvas problem and was not missing YOLO detections. The
database itself had zero overlay rows for the affected video bundles.

## Fix contract

- The first sidecar read may use the range cache for normal throughput.
- Once the reader proves that `latest_frame_pts` is behind the event anchor,
  every retry must bypass the range cache and read the latest per-source Redis
  stream without an event-time-derived stream-ID upper bound. Runtime epoch,
  stream session, source, camera, wall-clock window, and payload frame PTS
  filters still constrain the accepted messages.
- The default bounded anchor wait is 120 seconds, below the 600-second frame
  annotation TTL and the evidence annotation deadline.
- Retry cache bypass is recorded in `annotation_anchor_wait` as
  `range_cache_bypassed_for_retry=true` for audit.
- If the bounded 120-second Redis retry still cannot recover the anchor,
  media-worker may construct person-context overlay rows from retained
  `person_bbox_observations`, but only when observation `frame_pts` exactly
  matches a canonical evidence timeline frame. This remains a DB-backed YOLO
  observation path, not a frontend-generated or synthetic bbox fallback.
- Video, timeline, overlay rows, trajectory records, trajectory JPEGs, events,
  and evidence bundles remain retained after the pressure run.

## Acceptance

For every retained video evidence bundle:

1. `raw_clip.mov` is playable.
2. `evidence_frame_timeline` has rows.
3. The DB-backed annotations endpoint reports non-zero displayable bbox rows.
4. Person context is present and no filesystem annotation fallback is used.
5. A long-run audit compares total video bundles, timeline-covered bundles, and
   overlay-covered bundles; equality is required before the bbox gate passes.

Regression coverage lives in
`harness/tests/test_media_worker_perf_safety.py::test_frame_cache_sidecar_retries_when_exporter_is_behind`.

## Live production proof

After deploying the retry fixes and exact-PTS DB recovery during the same
two-hour run, an intermediate port-8090 audit reached 411/411 video bundles
with DB overlay rows. Newly finalized post-fix bundles produced 9 and 10
overlay frames directly, proving that the result was not limited to the
one-time historical backfill.

The final artifact is:

`/data/video-analytics/artifacts/pressure60_4p1_dual1gpu_retain2h_20260711T034003Z`

Its final DB-backed 8090 audit reported:

- 3,559 playable retained bundles: 553 intrusion videos and 3,006 watchlist
  images;
- 553/553 videos with a DB timeline and 553/553 with displayable DB
  annotations;
- `annotation_bbox_missing_count=0` and
  `annotation_person_context_missing_count=0`;
- `annotation_fallback_count=0`, `timeline_missing_count=0`, and
  `timeline_fallback_count=0`;
- zero retained intrusion videos without a non-empty
  `evidence_overlay_segments` row;
- 553/553 video windows and durations passed: 252 at 5:5, 189 at 10:10, and
  112 at 15:15, with no duration mismatch or unexpected policy;
- a retained video sampled through port 8090 returned HTTP 200 and was readable
  by `ffprobe`.

Trajectory retention was verified independently of evidence overlays. The run
retained 283,855 face observations across 60/60 sources, including 3,898 rows
with a persisted trajectory face-crop path. Sample trajectory JPEGs served by
port 8090 returned HTTP 200. Cleanup disabled the 60 temporary cameras but
deleted zero cameras, events, face observations, person bbox observations,
rules, zones, or evidence paths. The normal single-Savant runtime was restored
healthy and no pressure source or dual-shard container remained.

## Two-hour pressure result

The bbox and visual-retention acceptance gates passed, but the overall
two-hour pressure gate did not pass. The final run contained 24,149 events
(19,587 intrusion and 4,562 watchlist hits), 9,016 unsuppressed evidence tasks,
3,252 materialized tasks, 5,260 expired tasks, and 504 failed tasks. The report
status was `failed_pressure_gates` with these reasons:

- `materialization_expired_present`;
- `savant_send_failures`;
- `steady_effective_fps_below_minimum`;
- `forwarder_queue_full` and `forwarder_queue_nonzero`;
- `pressure_observed_window_exceeded`;
- `validate_seq_iq_exceeded`;
- `rolling_cache_missing_source_segments`;
- `rolling_cache_segment_fps_below_full_rate`;
- `rolling_cache_segments_unmeasured`.

The event and trajectory counts continued growing after video materialization
had saturated at 553 playable intrusion clips. This separates the fixed bbox
completeness defect from the remaining sustained evidence-throughput and
forwarder/FPS capacity failures. A run must not be called pressure-gate-passed
solely because all materialized videos have correct bbox overlays.
