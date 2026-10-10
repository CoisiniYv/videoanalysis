#!/usr/bin/env python3
"""Pull one H.264 RTSP stream and share it with local pressure-test readers.

Run in the existing Savant DeepStream image (GstRtspServer is already bundled).
There is no decoding, encoding, frame-rate selection, or evidence handling here.
Source-adapter containers still assign independent source IDs to all test lanes.
"""
import argparse
import json
import signal
import threading
from datetime import datetime
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo


class SharedRtspRelay:
    def __init__(self, source, *, address='127.0.0.1', port=18554,
                 path='/live/1080movie', latency_ms=200, log=print):
        import gi
        gi.require_version('Gst', '1.0')
        gi.require_version('GstRtsp', '1.0')
        gi.require_version('GstRtspServer', '1.0')
        from gi.repository import GLib, Gst, GstRtsp, GstRtspServer

        if urlsplit(source).scheme not in ('rtsp', 'rtsps'):
            raise ValueError('source must be an RTSP or RTSPS URL')
        if not 0 < port < 65536 or latency_ms < 0:
            raise ValueError('invalid port or latency')
        if not path.startswith('/') or '?' in path or '#' in path:
            raise ValueError('path must be an absolute RTSP mount path')
        Gst.init(None)
        self._GLib, self._Gst = GLib, Gst
        self._log, self._lock = log, threading.Lock()
        self._source, self._latency_ms = source, latency_ms
        self._media_created = self._active_media = self._clients = 0
        self._server = GstRtspServer.RTSPServer.new()
        self._server.set_address(address)
        self._server.set_service(str(port))
        self._factory = GstRtspServer.RTSPMediaFactory.new()
        self._factory.set_shared(True)
        self._factory.set_protocols(GstRtsp.RTSPLowerTrans.TCP)
        # Set the URI as a property in media-configure; never interpolate an
        # arbitrary URI (or credentials) into GStreamer's pipeline language.
        self._factory.set_launch(
            '( rtspsrc name=upstream protocols=tcp drop-on-latency=false ! '
            'rtph264depay ! h264parse ! '
            'rtph264pay name=pay0 pt=96 config-interval=1 )'
        )
        self._factory.connect('media-configure', self._configure_media)
        self._server.get_mount_points().add_factory(path, self._factory)
        self._server.connect('client-connected', self._client_connected)
        self._source_id = self._server.attach(None)
        if not self._source_id:
            raise RuntimeError('could not bind RTSP relay listener')
        self._timer_id = GLib.timeout_add_seconds(15, self._emit_stats)
        self._loop = GLib.MainLoop()
        self._emit('relay_ready', address=address, port=port, path=path,
                   upstream_host=urlsplit(source).hostname, shared=True)

    def _emit(self, event, **values):
        self._log(json.dumps({'time_seoul':datetime.now(ZoneInfo('Asia/Seoul')).isoformat(),
                             'event':event, **values}))

    def _configure_media(self, _factory, media):
        source = media.get_element().get_by_name('upstream')
        source.set_property('location', self._source)
        source.set_property('latency', self._latency_ms)
        with self._lock:
            self._media_created += 1
            self._active_media += 1
        media.connect('unprepared', self._media_unprepared)
        self._emit('upstream_media_created', **self.stats())

    def _media_unprepared(self, _media):
        with self._lock:
            self._active_media = max(0, self._active_media - 1)
        self._emit('upstream_media_unprepared', **self.stats())

    def _client_connected(self, _server, client):
        with self._lock:
            self._clients += 1
        client.connect('closed', self._client_closed)

    def _client_closed(self, _client):
        with self._lock:
            self._clients = max(0, self._clients - 1)

    def stats(self):
        with self._lock:
            return {'upstream_media_created':self._media_created,
                    'active_upstream_media':self._active_media,
                    'connected_clients':self._clients}

    def _emit_stats(self):
        self._emit('relay_stats', **self.stats())
        return True

    def run(self):
        self._loop.run()

    def stop(self):
        self._GLib.source_remove(self._source_id)
        self._GLib.source_remove(self._timer_id)
        self._loop.quit()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True)
    parser.add_argument('--address', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=18554)
    parser.add_argument('--path', default='/live/1080movie')
    parser.add_argument('--latency-ms', type=int, default=200)
    args = parser.parse_args()
    relay = SharedRtspRelay(args.source, address=args.address, port=args.port,
                            path=args.path, latency_ms=args.latency_ms,
                            log=lambda line: print(line, flush=True))
    signal.signal(signal.SIGTERM, lambda *_: relay.stop())
    signal.signal(signal.SIGINT, lambda *_: relay.stop())
    relay.run()


if __name__ == '__main__':
    main()
