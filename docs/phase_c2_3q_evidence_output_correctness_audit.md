# C2.3Q Evidence Output Correctness Audit

## Result

Result marker: `PASS_C2_3Q_WITH_POSE_CLIPPING_WARNINGS`

C2.3Q audited the latest C2.3A post-Savant evidence bundle for visual and structural correctness. Automatic audit output marked several sampled frames as suspicious because pose keypoints extended outside the image or outside an inflated person bbox. Manual review confirmed these warnings are acceptable clipping, low-confidence, or edge-pose cases and do not block C2.3B.

## Inputs

- Audit bundle path: `/data/video-analytics/media/evidence/c2_3a_20260607T161416`
- Audit output path: `/data/video-analytics/media/evidence_audit/c2_3q_20260607T164710`
- HTML review page: `/data/video-analytics/media/evidence_audit/c2_3q_20260607T164710/index.html`
- Contact sheet: `/data/video-analytics/media/evidence_audit/c2_3q_20260607T164710/contact_sheet.jpg`
- Audit summary: `/data/video-analytics/media/evidence_audit/c2_3q_20260607T164710/audit_summary.json`

## Audit Summary

- Timeline: PASS
  - `original_metadata_frame_count=240`
  - `decoded_video_frame_count=240`
  - `sidecar_frame_count=240`
  - `trim_occurred=false`
  - `timeline_reconciliation_status=frame_counts_match`
- Fallback: PASS
  - `fallback_used=false`
  - `legacy_used_for_visual_binding=false`
  - `annotation_source_kind=production_sidecar`
- Bbox geometry: PASS
  - sampled object boxes were parseable as pixel `xyxy`
  - sampled object boxes had positive width and height
  - sampled object boxes were in image bounds
- Face landmarks: PASS
  - sampled face landmarks were present where face objects were present
  - sampled face landmarks were in image bounds and visually aligned to faces
- Identity semantics: PASS
  - `known_face_count=0` is expected before C2.4
  - C2.3Q does not claim gallery recognition, watchlist proof, or known identity binding
- Pose keypoints: PASS_WITH_WARNINGS
  - automatic audit warnings were limited to keypoints outside image bounds or away from an inflated person bbox
  - manual review accepted these as clipping, low-confidence, or edge-pose warnings

## Manual Review

Manual review was completed using:

- `/data/video-analytics/media/evidence_audit/c2_3q_20260607T164710/index.html`

Reviewed suspicious frames:

- `30`
- `120`
- `180`
- `210`
- `239`

Manual conclusion:

- person bbox alignment is acceptable
- face bbox alignment is acceptable
- face landmarks align with the visible faces
- overall pose overlay is visually accurate enough for the current C2 evidence path
- pose keypoint clipping warnings are non-blocking

## Identity Boundary

C2.3Q remains an evidence content correctness audit. It does not connect identity binding.

- ordinary face observations must remain unknown faces
- `known_face_count=0` is expected before C2.4
- no person name should be displayed from this audit
- no gallery match or watchlist visual proof is claimed

## Next Step

C2.3Q is closed with `PASS_C2_3Q_WITH_POSE_CLIPPING_WARNINGS`.

The next phase is C2.3B: Event / Clip Post-Savant Replay Stream Mapping.
