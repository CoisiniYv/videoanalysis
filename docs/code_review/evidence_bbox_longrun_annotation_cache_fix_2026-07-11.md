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
