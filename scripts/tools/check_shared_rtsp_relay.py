#!/usr/bin/env python3
"""Native CPU integration check: two decoded clients share one upstream RTSP."""
import json

import gi
gi.require_version('Gst', '1.0')
gi.require_version('GstRtspServer', '1.0')
from gi.repository import GLib, Gst, GstRtspServer

from run_shared_rtsp_relay import SharedRtspRelay


def main():
    Gst.init(None)
    source = GstRtspServer.RTSPServer.new()
    source.set_address('127.0.0.1')
    source.set_service('18555')
    factory = GstRtspServer.RTSPMediaFactory.new()
    factory.set_shared(True)
    factory.set_launch(
        '( videotestsrc is-live=true ! '
        'video/x-raw,width=160,height=120,framerate=8/1 ! '
        'x264enc tune=zerolatency speed-preset=ultrafast key-int-max=8 ! '
        'rtph264pay name=pay0 pt=96 )'
    )
    upstream_sessions = []
    factory.connect('media-configure', lambda _factory, media: upstream_sessions.append(media))
    source.get_mount_points().add_factory('/test', factory)
    source_id = source.attach(None)
    assert source_id
    relay = SharedRtspRelay('rtsp://127.0.0.1:18555/test', port=18556,
                            path='/test', latency_ms=20, log=lambda _: None)
    clients = []
    counts = [0, 0]
    observed = {}
    errors = []

    def got_sample(sink, index):
        sample = sink.emit('pull-sample')
        assert sample is not None and sample.get_buffer().get_size() > 0
        counts[index] += 1
        return Gst.FlowReturn.OK

    def bus_message(_bus, message):
        if message.type == Gst.MessageType.ERROR:
            error, _ = message.parse_error()
            errors.append(str(error))

    for index in range(2):
        client = Gst.parse_launch(
            'rtspsrc location=rtsp://127.0.0.1:18556/test protocols=tcp latency=20 ! '
            'rtph264depay ! h264parse ! avdec_h264 ! '
            'appsink name=frames emit-signals=true sync=false'
        )
        client.get_by_name('frames').connect('new-sample', got_sample, index)
        bus = client.get_bus()
        bus.add_signal_watch()
        bus.connect('message', bus_message)
        clients.append(client)
        assert client.set_state(Gst.State.PLAYING) != Gst.StateChangeReturn.FAILURE

    def finish():
        observed.update(relay.stats())
        relay.stop()
        return False

    GLib.timeout_add_seconds(6, finish)
    try:
        relay.run()
    finally:
        for client in clients:
            client.set_state(Gst.State.NULL)
        GLib.source_remove(source_id)
    assert not errors, errors
    assert min(counts) >= 8, counts
    assert len(upstream_sessions) == 1, len(upstream_sessions)
    assert observed == {'upstream_media_created':1, 'active_upstream_media':1,
                        'connected_clients':2}, observed
    print(json.dumps({'passed':True, 'decoded_frames_per_client':counts,
                      'upstream_server_sessions':len(upstream_sessions), **observed}))


if __name__ == '__main__':
    main()
