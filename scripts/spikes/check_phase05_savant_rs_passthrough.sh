#!/usr/bin/env bash
set -euo pipefail

IMAGE="${SAVANT_IMAGE:-ghcr.io/insight-platform/savant-deepstream:0.6.0-7.1}"

docker run --rm --entrypoint bash "$IMAGE" -lc 'python - <<'"'"'PY'"'"'
import time

from savant_rs.primitives import VideoFrame, VideoFrameContent
from savant_rs.py.utils.zeromq import ZeroMQSource
from savant_rs.utils.serialization import save_message_to_bytes
from savant_rs.zmq import BlockingWriter, WriterConfigBuilder


def start_endpoint(endpoint):
    endpoint.start()
    return endpoint


source_id = "phase05_probe"
content = b"\x00\x00\x00\x01phase05-h264-payload"
frame = VideoFrame(
    source_id=source_id,
    framerate="30/1",
    width=16,
    height=16,
    codec="h264",
    content=VideoFrameContent.external("zeromq", None),
    keyframe=True,
    time_base=(1, 10**9),
    pts=123456789,
    dts=123456000,
    duration=33333333,
)

in_reader = start_endpoint(
    ZeroMQSource("router+bind:tcp://127.0.0.1:39101", receive_timeout=100, receive_hwm=10)
)
out_reader = start_endpoint(
    ZeroMQSource("router+bind:tcp://127.0.0.1:39102", receive_timeout=100, receive_hwm=10)
)
in_writer = start_endpoint(
    BlockingWriter(WriterConfigBuilder("dealer+connect:tcp://127.0.0.1:39101").build())
)
out_writer = start_endpoint(
    BlockingWriter(WriterConfigBuilder("dealer+connect:tcp://127.0.0.1:39102").build())
)

try:
    time.sleep(0.2)
    send_result = in_writer.send_message(source_id, frame.to_message(), content)
    print("input_send_result", type(send_result).__name__)

    inbound = None
    for _ in range(50):
        inbound = in_reader.next_message()
        if inbound is not None:
            break
        time.sleep(0.05)
    assert inbound is not None, "inbound message not received"

    inbound_frame = inbound.message.as_video_frame()
    inbound_bytes = save_message_to_bytes(inbound.message)
    assert inbound.content == content, "inbound content mismatch"
    assert inbound_frame.source_id == source_id
    assert inbound_frame.pts == 123456789
    assert inbound_frame.dts == 123456000
    assert inbound_frame.duration == 33333333
    assert inbound_frame.keyframe is True
    assert inbound_frame.codec == "h264"
    assert inbound_frame.uuid == frame.uuid

    forward_result = out_writer.send_message(source_id, inbound.message, inbound.content)
    print("forward_send_result", type(forward_result).__name__)

    outbound = None
    for _ in range(50):
        outbound = out_reader.next_message()
        if outbound is not None:
            break
        time.sleep(0.05)
    assert outbound is not None, "outbound message not received"

    outbound_frame = outbound.message.as_video_frame()
    outbound_bytes = save_message_to_bytes(outbound.message)
    assert outbound.content == content, "outbound content mismatch"
    assert outbound_bytes == inbound_bytes, "message bytes changed across passthrough"
    assert outbound_frame.uuid == inbound_frame.uuid
    assert outbound_frame.source_id == inbound_frame.source_id
    assert outbound_frame.pts == inbound_frame.pts

    print("PASS_PHASE05_S2_MINIMAL_PASSTHROUGH", outbound_frame.uuid, len(outbound.content))
finally:
    for endpoint in (in_writer, out_writer, in_reader, out_reader):
        try:
            endpoint.shutdown()
        except AttributeError:
            endpoint.terminate()
        except Exception:
            pass
PY'
