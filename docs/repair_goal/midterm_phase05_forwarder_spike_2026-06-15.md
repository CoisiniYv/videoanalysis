# Midterm Phase 0.5 Forwarder Spike

Date: 2026-06-15

## Scope

Phase 0.5 answers the hard-gate questions from
`specs/16_dual_path_30x2_t4_production_optimization.md` before implementing a
production `analysis-forwarder`:

- S1: whether compressed-frame sampling before Savant decode is equivalent to
  the current in-Savant gate.
- S2: whether a forwarder image can use `savant_rs` to read and write ZMQ
  `VideoFrame` messages without changing UUID/PTS/content.

No production topology was changed in this phase.

## S1 Result: Gate Is Before Decode

Result: pass.

Current deployed module config:

```yaml
pipeline:
  source:
    element: zeromq_source_bin
    ingress_frame_filter:
      module: custom.filters.pts_fps_gate
      class_name: PtsFpsGate
```

Code inspection inside the running Savant container:

- `/opt/savant/src/module/savant_patches/v0.6.0/pipeline.py`
  - `_add_source()` sets `add_frames_to_pipeline=False` for
    `zeromq_source_bin`.
  - `_add_input_converter()` inserts `savant_rs_add_frames` only when
    `add_frames_to_pipeline=True`, which is not the current ZMQ source path.
- `/opt/savant/gst_plugins/python/zeromq_src.py`
  - `handle_video_frame()` calls `self.ingress_pyfunc(video_frame)` before
    `build_frame_buffer(video_frame, external_content)`.
  - `build_frame_buffer()` is where external ZMQ content becomes a
    `Gst.Buffer`.

Therefore a frame rejected by `PtsFpsGate` is not converted into a GStreamer
buffer and cannot reach the downstream DeepStream decode/mux path. The current
runtime is already decoding the sampled H264 subset, not decoding every Replay
frame and dropping after decode.

Runtime corroboration:

```text
component=savant_security_pts_fps_gate_tick source_id=primary_rtsp seen=56400 accepted=18951 enabled=True max_fps=8
component=savant_security_pts_fps_gate_tick source_id=source_00000000-0000-4000-8000-781078565686 seen=70500 accepted=18802 enabled=True max_fps=8
```

8090 overview at the same time showed both sources live and annotated:

```text
primary_rtsp effective_fps=8.1 last_frame_age=0.046s frame_annotations_exported_total=18956
source_00000000-0000-4000-8000-781078565686 effective_fps=8.1 last_frame_age=0.0s frame_annotations_exported_total=18839
```

Conclusion: a Phase 1 forwarder may move the same `PtsFpsGate` decision upstream
of Savant without introducing a new H264 decode mode, as long as it forwards the
same accepted `VideoFrame` message and external content bytes verbatim. The
forwarder must not re-encode, rewrite UUID/PTS, or do random non-GOP-aware packet
dropping.

## S2 Result: Savant Image Can Passthrough `VideoFrame`

Result: pass.

Image check:

```text
ghcr.io/insight-platform/savant-deepstream:0.6.0-7.1
savant_rs OK /opt/venv/lib/python3.12/site-packages/savant_rs/__init__.py
savant_rs.zmq OK builtin
savant_rs.primitives OK builtin
savant_rs.py.utils.zeromq OK /opt/venv/lib/python3.12/site-packages/savant_rs/py/utils/zeromq.py
```

The spike script
`scripts/spikes/check_phase05_savant_rs_passthrough.sh` creates a synthetic
external-content H264 `VideoFrame`, sends it through a local ROUTER/DEALER input
pair, forwards the exact received message and content through a second
ROUTER/DEALER pair, then asserts:

- inbound content bytes match original content
- inbound UUID/source/PTS/DTS/duration/keyframe/codec match the original frame
- outbound serialized message bytes match inbound serialized message bytes
- outbound content bytes match inbound content bytes
- outbound UUID/source/PTS match inbound metadata

Observed output:

```text
input_send_result WriterResultSuccess
forward_send_result WriterResultSuccess
PASS_PHASE05_S2_MINIMAL_PASSTHROUGH 019ec6ff-df77-7cb2-83e6-f888d52ecc0a 24
```

Conclusion: the Phase 1 forwarder image should be based on the Savant DeepStream
image, or another image that explicitly carries a compatible `savant_rs`. Do not
use plain `python:3.12-slim` unless a pinned compatible `savant_rs` wheel is
added and tested.

## PASS Token

`PASS_PHASE05_SPIKE`

Phase 1 is unblocked with these implementation constraints:

- build the forwarder on the Savant image or another proven `savant_rs` image;
- reuse the current PTS-domain `PtsFpsGate` logic;
- always pass keyframes;
- forward accepted `VideoFrame` messages and external content bytes verbatim;
- bound the outgoing queue and drop analysis frames on overflow rather than
  blocking Replay's read side;
- keep full-rate evidence in Replay unchanged.

## Validation

Executed:

```bash
scripts/spikes/check_phase05_savant_rs_passthrough.sh
docker compose -f infra/docker-compose.midterm.yml config
git diff --check
```
