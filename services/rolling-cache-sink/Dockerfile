FROM ghcr.io/insight-platform/savant-adapters-gstreamer:0.6.0

COPY services/rolling-cache-sink/requirements.txt /tmp/rolling-cache-sink-requirements.txt
RUN python -m pip install --no-cache-dir \
    -r /tmp/rolling-cache-sink-requirements.txt

COPY services/rolling-cache-sink/app /opt/rolling-cache-sink/app
COPY scripts/runtime/rolling_cache_sink_entrypoint.sh /opt/rolling-cache-sink-entrypoint.sh
COPY scripts/runtime/rolling_cache_maintenance.py /opt/rolling-cache-maintenance.py

RUN chmod 0555 /opt/rolling-cache-sink-entrypoint.sh \
    && chmod -R a-w /opt/rolling-cache-sink/app

HEALTHCHECK --interval=5s --timeout=3s --start-period=30s --retries=12 \
  CMD python -c "import os, urllib.request; port=os.getenv('ROLLING_CACHE_SINK_HTTP_PORT', '8080'); urllib.request.urlopen(f'http://127.0.0.1:{port}/readyz', timeout=2).read()"

ENTRYPOINT ["/bin/sh", "/opt/rolling-cache-sink-entrypoint.sh"]
