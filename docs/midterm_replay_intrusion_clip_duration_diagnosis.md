# Midterm Replay Intrusion Clip Duration Diagnosis

## Status

Date: 2026-06-10

This note captures the runtime diagnosis for intrusion evidence bundles whose
`raw_clip.mov` is longer than the expected event window, plus the current
working-tree mitigation.

Update 2026-06-15: the duration-guard conclusions remain current, but the
`MAX_FPS_CONTROL=true` line below is the 2026-06-10 runtime snapshot. Current
midterm config keeps `MAX_FPS=8/1` and `INGRESS_FPS_GATE_ENABLED=true`, but
sets `MAX_FPS_CONTROL=false` after the Phase 0A backpressure experiment. Current
topology also includes `analysis-forwarder` before Savant. See
`docs/current_mainline_status.md` for current runtime shape.

The 2026-06-10 midterm evidence configuration still requested event-window
clips:

```text
EVIDENCE_TOPOLOGY=post_savant_replay
FRAME_CACHE_TIME_DOMAIN_CROP_ENABLED=true
DEFAULT_PRE_SECONDS=5
DEFAULT_POST_SECONDS=5
MAX_FPS_CONTROL=true
MAX_FPS=8/1
MIN_FPS=2/1
```

The configured target window is still 10 seconds: 5 seconds before the event
and 5 seconds after the event.

## Observed Evidence

Recent intrusion evidence under `/data/video-analytics/media/evidence` shows
both successful and failed time-domain cropping in the same runtime.

| Evidence id | Event type | Clip duration | Crop applied | Crop failed | Result |
| --- | --- | ---: | --- | --- | --- |
| `df07fb8b-26cd-4f0b-8c83-5a0d79c429fb` | intrusion | 10.01s | true | false | good |
| `b5242c9c-7ac2-4e29-9695-01d51053eba9` | intrusion | 9.97s | true | false | good |
| `52851820-f83b-47f4-9321-c71dd581c13f` | intrusion | 9.97s | true | false | good |
| `e99262d0-a6a1-4235-b875-564029de1a56` | intrusion | 61.35s | false | true | bad |
| `2d9fca4a-8394-4956-9a4c-3f836275500c` | intrusion | 61.35s | false | true | bad |
| `72c875ea-a015-421c-91bf-beab4d24fe7e` | intrusion | 61.35s | false | true | bad |
| `2c4560ed-f3f8-49e5-8675-5d483ee777f6` | intrusion | 31.28s | false | true | bad |
| `49057d6d-23f7-4ebe-b319-5e5873e1f163` | intrusion | 31.28s | false | true | bad |
| `13e7ac35-08bb-478c-8062-8e1ce805a0d8` | intrusion | 84.15s | false | true | bad |

At the time of diagnosis, the bad bundles were marked as unverified, but their
overlong raw clips were still published in the evidence directory.

## Why It Happens

The failure mode is not that the requested evidence window changed to 31, 61, or
84 seconds. The requested window remains 10 seconds.

The bad bundles fail while selecting sink metadata rows for the requested PTS
window:

```text
ValueError:time_domain_crop_selected_zero_metadata_frames
```

Examples:

```text
72c875ea...
requested window: 98.747s..108.747s
sink metadata:    38.413s..69.620s
result:           no metadata frame in requested window

2c4560ed...
requested window: 9.965s..19.965s
sink metadata:    52.677s..94.093s
result:           no metadata frame in requested window

b5242c9c...
requested window: 77.893s..87.893s
sink metadata:    77.920s..87.888s
result:           crop succeeds
```

Before the current fix, when selection returned zero rows, `media-worker` fell
back to copying the source Replay sink output into `raw_clip.mov`. That fallback
is what exposed the full Replay output duration:

```text
services/media-worker/app/worker.py
frame_cache_time_domain_crop_failed -> shutil.copy2(source_video, raw_clip_path)
```

## Restart Versus Configuration

This is not explained by the crop configuration being disabled. Successful
10-second clips exist in the same runtime, with the same media-worker crop
configuration.

Recent runtime state showed:

```text
media-worker  started 2026-06-09T11:49:27Z, restartCount=0
clip-worker   started 2026-06-09T09:41:43Z, restartCount=0
savant        started 2026-06-10T09:46:29Z, restartCount=0
source-adapter started 2026-06-10T09:50:21Z, restartCount=1
```

Restart can trigger or amplify the issue by changing the PTS epoch, Replay
buffer content, or frame annotation cache alignment. However, the immediate
cause of the overlong evidence file is still the media-worker fallback that
publishes the full Replay sink output after time-domain crop failure.

## Current Risk

The diagnosed behavior was unsafe for production evidence:

- A failed crop can still publish a user-visible raw clip.
- `clip_status=generated_unverified` is not enough if the frontend still exposes
  the raw clip as evidence.
- Overlong clips can appear even though the requested event window remains 10
  seconds.
- The evidence bundle records `time_domain_crop_failed=true`, but does not
  fail closed at the media boundary.

## Current Working-Tree Fix

`services/media-worker/app/worker.py` now fails closed for this failure class:

- `frame_cache_time_domain_crop_failed` deletes any partial `raw_clip.mov` and
  does not copy the full Replay sink video into the evidence bundle.
- post-Savant finalization compares the final clip duration to
  `requested_duration_s + POST_SAVANT_DURATION_GUARD_SLACK_SEC`.
- the default post-Savant production slack is 1 second.
- the finalizer also checks sink metadata coverage and continuity before DB
  update: event PTS must be inside the final sink metadata window, the event
  must be centered near the requested pre-window, and large/non-monotonic PTS
  gaps fail closed.
- the zero-metadata fallback that cropped from clip start without sink metadata
  proof is disabled; zero selected metadata frames now fail closed.
- failed crop or overlong final clips are marked
  `clip_status=duration_guard_failed`, `production_ready=false`, and
  `raw_clip_path=""` in `metadata.json` / DB bundle payload.
- diagnostic `summary.json`, `sink_metadata.json`, and sidecar output are still
  preserved.

Focused coverage is in
`harness/tests/test_midterm_replay_evidence_duration_guard.py`.

## Required Fix Direction

The remaining evidence-layer direction is:

1. Keep failed crop, overlong final clips, and sink-window mismatches
   fail-closed.
2. Treat restart or source epoch changes as separate runtime epochs; stale
   Replay/sink/frame-cache data must not be mixed with current evidence jobs.

The implementation plan is tracked in
`specs/14_replay_evidence_duration_guard_fix.md`.
