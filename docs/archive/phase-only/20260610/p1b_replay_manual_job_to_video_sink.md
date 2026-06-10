# Phase P1b — Replay Manual Job to Video File Sink

Status: **VERIFIED** — manual Replay job produced Video File Sink output.

P1b starts from the P1a inline topology and adds only an on-demand Video File
Sink for a manual Replay REST job. It does not add clip orchestration or media
finalization workers.

## Topology

```text
testVideo/test.mp4
  -> source-adapter
  -> replay-service
  -> savant-security
  -> Redis security.events

manual Replay job, using anchor keyframe_uuid
  -> replay-service
  -> video-file-sink
  -> /data/video-analytics/media/p1b-replay-manual-sink/<run_id>/...
```

The P1a stream remains single path:

```text
source-adapter -> replay-service -> savant-security
```

The P1b media path is created only by `PUT /api/v1/job`:

```text
manual Replay job -> video-file-sink
```

## Runtime Configuration

| Field | Value |
|---|---|
| Replay API URL | `http://127.0.0.1:8086` |
| Replay input stream | `router+bind:tcp://0.0.0.0:5555` |
| Source adapter output | `dealer+connect:tcp://replay-service:5555` |
| Replay inline output | `dealer+connect:tcp://savant-security:5557` |
| Savant input stream | `router+bind:tcp://0.0.0.0:5557` |
| Manual job sink URL | `pub+connect:tcp://video-file-sink:6666` |
| Video File Sink input | `sub+bind:tcp://0.0.0.0:6666` |
| Source ID | `p1a_replay_inline` |
| Camera ID | `cam_p1a_replay_inline` |
| Default anchor keyframe_uuid | `019e7c8d-b9e8-7ed1-92bf-a09384777df8` |
| Default offset | `5` seconds |
| Default stop condition | `300` frames, about 10 seconds at 30 FPS |

## Files

- `infra/docker-compose.p1b-replay-manual-sink-poc.yml`
- `scripts/smoke/check_p1b_replay_manual_job_to_video_sink.sh`
- `harness/tests/test_p1b_replay_sink_contract.py`

## Commands

```bash
python3 -m pytest harness/tests/test_p1b_replay_sink_contract.py -q
bash scripts/smoke/check_p1b_replay_manual_job_to_video_sink.sh
git diff --check
git status --short
```

The smoke script starts the P1a inline services if needed, starts only the P1b
`video-file-sink`, verifies the fixed keyframe exists in Replay cache, creates a
manual Replay job with `anchor_keyframe`, waits for `metadata.json` and
`video.*`, and validates the video with `ffprobe`.

## Report Fields

The smoke output includes the Replay API URL, anchor keyframe_uuid, Replay job
ID, Replay job request, sink output directory, metadata.json path, video file
path, video size, video duration, video format, and boundary confirmations.

## Verification Result

Last verified on 2026-05-31 with:

| Field | Value |
|---|---|
| Smoke result | `22 passed, 0 failed` |
| Replay job id | `019e7ca3-40aa-7562-8df5-24eab4de72a2` |
| Sink output directory | `/media/p1b-replay-manual-sink/1780207529864358505/p1b_manual_1780207529864358505%/test%` |
| Host output directory | `/data/video-analytics/media/p1b-replay-manual-sink/1780207529864358505/p1b_manual_1780207529864358505%/test%` |
| metadata.json | `118160` bytes, 301 lines |
| video file | `video.mov`, 2130736 bytes |
| video duration | `5.966667` seconds |
| video format | `mov,mp4,m4a,3gp,3g2,mj2` |

## Replay Job Request

The smoke builds the request with these key fields:

```json
{
  "sink": {
    "url": "pub+connect:tcp://video-file-sink:6666"
  },
  "configuration": {
    "stored_stream_id": "p1a_replay_inline",
    "resulting_stream_id": "p1b_manual_<run_id>",
    "send_metadata_only": false,
    "send_eos": true,
    "labels": {
      "phase": "p1b",
      "source_id": "p1a_replay_inline",
      "anchor_keyframe_uuid": "019e7c8d-b9e8-7ed1-92bf-a09384777df8"
    }
  },
  "stop_condition": {
    "frame_count": 300
  },
  "anchor_keyframe": "019e7c8d-b9e8-7ed1-92bf-a09384777df8",
  "offset": {
    "seconds": 5
  }
}
```

## Boundaries

- No clip-worker.
- No media-worker.
- No event-worker or DB write.
- No annotated clip generation.
- No production compose change.
- No second RTSP pull.
- No source extraction fallback.
- No `ffmpeg` clip cutting; `ffprobe` is used only to validate the sink output.
- No media artifacts are committed.
