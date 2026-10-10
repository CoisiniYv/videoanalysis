#!/usr/bin/env python3
"""CPU-only integration check inside the pinned Savant image; no RTSP/GPU needed."""
import importlib.util
import json
import logging
from pathlib import Path

from pygstsavantframemeta import gst_buffer_add_savant_frame_meta
from savant.gstreamer import Gst
from savant_rs.pipeline2 import (
    StageFunction, VideoPipeline, VideoPipelineConfiguration, VideoPipelineStagePayloadType,
)
from savant_rs.primitives import VideoFrame, VideoFrameContent


def main():
    root = Path(__file__).resolve().parents[2]
    path = root / 'modules/savant_security/savant_patches/v0.6.0/decoded_frame_guard.py'
    spec = importlib.util.spec_from_file_location('guard_native_check', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    Gst.init(None)
    stages = [(name, VideoPipelineStagePayloadType.Frame, StageFunction.none(), StageFunction.none())
              for name in ('decode', 'source-convert', 'source-capsfilter', 'muxer')]
    pipeline = VideoPipeline('decoded-frame-guard-check', stages, VideoPipelineConfiguration())

    def add_frame():
        frame = VideoFrame(source_id='probe', framerate='24/1', width=16, height=16,
                           codec='h264', content=VideoFrameContent.external('zeromq', None),
                           keyframe=True, time_base=(1, 10**9), pts=1_000_000,
                           dts=1_000_000, duration=41_666_667)
        return pipeline.add_frame('decode', frame), str(frame.uuid)

    # Use real pads, buffers, metadata and the streaming callback (no mock Rust).
    source = Gst.Pad.new('source', Gst.PadDirection.SRC)
    sink = Gst.Pad.new('sink', Gst.PadDirection.SINK)
    received = []
    sink.set_chain_function_full(lambda pad, parent, buffer: (received.append(buffer.pts), Gst.FlowReturn.OK)[1])
    assert source.link(sink) == Gst.PadLinkReturn.OK
    source.set_active(True)
    sink.set_active(True)
    module.add_decoded_frame_guard(source, pipeline, 'probe', logging.getLogger('guard-check'))
    source.push_event(Gst.Event.new_stream_start('guard-check'))
    segment = Gst.Segment.new()
    segment.init(Gst.Format.TIME)
    source.push_event(Gst.Event.new_segment(segment))

    def push(index):
        buffer = Gst.Buffer.new_allocate(None, 16, None)
        buffer.pts = len(received) + 1
        if index is not None:
            gst_buffer_add_savant_frame_meta(buffer, index)
        assert source.push(buffer) == Gst.FlowReturn.OK

    index, uuid = add_frame()
    push(index)
    assert len(received) == 1
    push(index)  # Duplicate while original has not yet reached muxer.
    assert len(received) == 1
    pipeline.move_as_is('muxer', [index])
    push(index)
    stale, _ = add_frame()
    pipeline.move_as_is('muxer', [stale])
    push(stale)  # Not in guard history; safe binding must reject backward move.
    push(None)
    assert len(received) == 1
    assert str(pipeline.get_independent_frame(index)[0].uuid) == uuid
    fresh, _ = add_frame()
    push(fresh)  # Streaming continues after rejection.
    assert len(received) == 2
    assert pipeline.get_stage_queue_len('muxer') == 2
    print(json.dumps({'passed': True, 'received': len(received), 'dropped': 4,
                      'original_uuid_preserved': True, 'alive_after_backward_move': True}))


if __name__ == '__main__':
    main()
