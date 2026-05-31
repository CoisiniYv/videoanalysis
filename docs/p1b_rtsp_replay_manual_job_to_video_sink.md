# P1b-RTSP Replay Manual Job to Video File Sink

Status: VERIFIED

This POC verifies the real RTSP input path only:

```text
source-adapter -> replay-service -> savant-security
manual Replay job -> video-file-sink
```

## Input

- input_type: rtsp
- input_uri: rtsp://10.37.57.112:8554/live/1080movie
- local_file_used: no
- test_video_used: no
- source_extraction_fallback: no

## Runtime Topology

- source-adapter -> replay-service: yes
- replay-service -> savant-security: yes
- single path source -> replay -> savant: yes
- second RTSP pull used: no

## Runtime

- replay-service: running
- savant-security: running
- source-adapter: running
- video-file-sink: running
- source_id: p1b_rtsp_replay
- camera_id: cam_p1b_rtsp_replay

## Event / Keyframe

- security.events observed: yes
- source_event_id: savant_security:cam_p1b_rtsp_replay:2:intrusion:1780214290316
- event_ts_ms: 1780214290358
- frame_uuid: 019e7d0a-5783-7911-9636-7426d2e198e2
- keyframe_uuid: 019e7d0a-437b-7cb3-8802-6773c8a1e1e8
- previous_keyframe_uuid: 019e7d0a-437b-7cb3-8802-6773c8a1e1e8

## Replay

- API URL: http://127.0.0.1:8087
- Replay job id: 019e7d0a-5ad2-7180-ac10-b949357c2ca0
- anchor keyframe uuid: 019e7d0a-437b-7cb3-8802-6773c8a1e1e8
- offset.seconds: 5
- stop_condition: frame_count=300
- sink.url: pub+connect:tcp://video-file-sink:6666
- stored_stream_id: p1b_rtsp_replay
- resulting_stream_id: p1b_rtsp_manual_1780214267619141257

## Sink Output

- sink output dir: /data/video-analytics/media/p1b-rtsp-replay-manual-sink/1780214267619141257/p1b_rtsp_manual_1780214267619141257%/unknown%
- metadata.json: /data/video-analytics/media/p1b-rtsp-replay-manual-sink/1780214267619141257/p1b_rtsp_manual_1780214267619141257%/unknown%/metadata.json
- video file: /data/video-analytics/media/p1b-rtsp-replay-manual-sink/1780214267619141257/p1b_rtsp_manual_1780214267619141257%/unknown%/video.mov
- video size: 3966587 bytes
- video duration: 5.296250 seconds

## Boundary

- boundary: no local file source
- no source extraction fallback
- no second RTSP pull
- no event-worker
- no clip-worker
- no media-worker
- no annotated_clip
- no production compose change
- no media artifacts committed
