# C1G.2c Evidence Bundle Boundary And Annotation Integrity

## Scope

C1G.2c only hardens evidence bundle finalization. It does not apply C1G.3
runtime changes, change the operator frontend, change the algorithm config API,
or change the Savant model chain.

## Root Causes

### 0 Byte `annotations.jsonl`

`media-worker` generated `annotations.jsonl` by opening the final file directly
and writing one JSONL row per continuous annotation record. When no matching
annotation record existed for the event window, the loop wrote no rows and left
a 0 byte file.

The old summary still set `frontend_overlay_required=true`, so an empty
timeline was advertised as a valid overlay source. `event_annotation.json` may
contain event-frame bbox/ROI information, but it is not the primary timeline
overlay source for the evidence viewer.

### 15 Minute Evidence Clip

The overlong bundle was generated for event
`cafeaa0c-ac91-4ed0-bee3-ad64314023ee`. Its record request and Replay job both
requested `pre_seconds=5` and `post_seconds=5`, with
`stop_condition.ts_delta_sec.max_delta_sec=10`. The runtime test duration
(`RUN_DURATION_SEC=900`) was not passed as `post_seconds`.

The sink metadata shows the Replay output continued for about 943 seconds. The
failure was therefore at the Replay/sink boundary: the bounded Replay job did
not stop at the requested `ts_delta_sec` duration, and `media-worker` copied the
result into an evidence bundle while only marking decode/duration corruption.

## Fixes

- `annotations.jsonl` is now written via a temporary file and atomic rename.
- Empty annotations now produce a non-overlay status record instead of a 0 byte
  file.
- `summary.json` now records `annotation_status`, `annotation_lines`,
  `overlay_available`, `frontend_overlay_required`, and an explicit empty or
  unavailable reason.
- Annotation generation failure is marked as
  `generated_annotation_failed` rather than clean `generated`.
- `media-worker` now applies a hard raw clip duration guard during bundle
  finalization.
- `summary.json`, `metadata.json`, and event payload media fields include guard
  results for later diagnosis.

## Duration Guard

The guard derives:

```text
expected_duration = requested pre_seconds + post_seconds
max_allowed_duration = expected_duration + EVIDENCE_MAX_DURATION_SLACK_SEC
```

Default:

```text
EVIDENCE_MAX_DURATION_SLACK_SEC=10
```

If `raw_clip_duration > max_allowed_duration`, the bundle status is
`duration_guard_failed`, and `summary.json` records:

- `duration_guard_failed=true`
- `duration_guard_status=failed`
- `duration_guard_reason=raw_clip_duration_exceeds_expected_plus_slack`
- `max_allowed_duration_seconds`

## Annotation Status

- `complete`: timeline overlay rows exist and `overlay_available=true`.
- `empty`: no supported annotation rows exist for the event window;
  `annotation_empty_reason` is required and `overlay_available=false`.
- `unavailable`: annotation generation failed; `annotation_unavailable_reason`
  is required and the bundle is marked `generated_annotation_failed`.

`annotation_lines` counts overlay timeline rows, not status records.

## State Relationship

- `alert`: emitted only for non-suppressed events.
- `record_request`: created only when the event is non-suppressed, clip is
  required, and runtime recording gates allow it.
- `evidence_task`: created for non-suppressed events that require evidence,
  even if a later recording gate suppresses the record request.
- `evidence_bundle`: created only after a record request becomes a Replay job
  and `media-worker` finalizes sink output.

This explains a runtime summary such as:

```text
non_suppressed_alerts: 28
record_requests_created: 1
evidence_tasks_created: 28
evidence_bundles_created: 1
```

The alerts and evidence tasks reflect all non-suppressed evidence-required
events. The single record request and single bundle reflect the configured
recording gates (`RECORDING_MAX_REQUESTS_PER_RUN=1`,
`CLIP_WORKER_MAX_JOBS_PER_RUN=1`, cooldown limits).

## Smoke

Run:

```bash
bash scripts/smoke/check_c1g2c_evidence_bundle_integrity.sh
```

The smoke writes:

```text
/data/video-analytics/artifacts/c1g2c/evidence_bundle_integrity_summary.json
```

It scans recent evidence bundles, diagnoses
`6208d5a0-0bc8-4660-9e09-b9309f12e67d`, fails if a 0 byte
`annotations.jsonl` is advertised as overlay-available, and fails if an
over-duration raw clip is still advertised as clean/generated evidence.
Over-duration historical bundles that are already non-clean are still reported
in `abnormal_bundles`.

## Known Limitations

- The guard prevents clean finalization of overlong future bundles; it does not
  repair or trim historical raw clips.
- `event_annotation.json` remains event-frame context and is not a substitute
  for timeline overlay rows.
- A source with decode corruption may still yield `generated_corrupt`; duration
  guard failure is recorded separately when the clip boundary is exceeded.
