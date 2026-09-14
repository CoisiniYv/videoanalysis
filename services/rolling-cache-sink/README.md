# Rolling Cache Sink

`rolling-cache-sink` stores short, immutable video fragments for the evidence pipeline. It consumes encoded H.264 streams from ZMQ, keeps one GStreamer pipeline per active source, rotates fragments on time/keyframe boundaries, and publishes completed segments for Media Worker.

The service is designed for the full-rate branch of the platform: AI inference may run at a lower sampling rate, while the rolling cache retains encoded video at the source rate so an event can later be materialized into a continuous clip.

## Build

The Docker build context is the repository root:

```bash
docker build -f services/rolling-cache-sink/Dockerfile .
```

## Configuration

`ZMQ_ENDPOINT` is required. The most commonly used environment variables are:

| Variable | Default | Purpose |
| --- | --- | --- |
| `ZMQ_ENDPOINT` | required | Input ZMQ endpoint |
| `ROLLING_CACHE_ROOT` | `/media/rolling-cache` | Cache root directory |
| `ROLLING_CACHE_NAMESPACE` | `midterm` | Namespace below the cache root |
| `ROLLING_CACHE_SEGMENT_SECONDS` | `4.0` | Target fragment duration |
| `ROLLING_CACHE_RUNTIME_EPOCH_ID` | generated/resolved | Explicit runtime epoch |
| `RUNTIME_EPOCH_STATE_PATH` | unset | Runtime epoch state file |
| `SOURCE_ID` | unset | Restrict the process to one source |
| `SOURCE_ID_PREFIX` | unset | Source filtering/prefix support |
| `ROLLING_CACHE_SINK_HTTP_HOST` | `0.0.0.0` | Health/metrics bind address |
| `ROLLING_CACHE_SINK_HTTP_PORT` | `8080` | Health/metrics HTTP port |
| `ROLLING_CACHE_PUBLICATION_WORKERS` | `1` | Publication worker count |
| `ROLLING_CACHE_PUBLICATION_FILE_SYNC_MODE` | `fsync` | File durability mode |
| `ROLLING_CACHE_PUBLICATION_METADATA_LAYOUT` | `split` | Published metadata layout |

See `app/config.py` for the complete set of validated options and bounds.

## Published Segment Layout

The default `split` layout is:

```text
<root>/<namespace>/epochs/<runtime_epoch_id>/<source_id>/segments/<segment_id>/
  video.mov
  metadata.json
  segment_manifest.json
```

`metadata.json` contains the Savant frame metadata used by downstream consumers. `segment_manifest.json` contains compact segment-level discovery data. A segment is considered published only after its files have passed the publication sequence and the final directory is visible.

## Publication Guarantees

Completed fragments are first written to a staging area outside the visible source segment directory. Publication then uses same-filesystem atomic rename together with explicit file/directory synchronization so Media Worker does not observe partially written segments.

The important consumer-facing properties are:

- published segment directories are immutable;
- incomplete or interrupted fragments remain outside the normal source segment tree;
- the default layout uses separate `metadata.json` and `segment_manifest.json` files;
- source and runtime epoch identifiers are validated before becoming path components;
- a process keeps a stable runtime epoch if the epoch-state file is temporarily unavailable during replacement.

Media Worker combines these published segments with evidence-task windows and read pins. Cache maintenance may remove expired segments, but active reads are protected by the read-pin mechanism used by the evidence pipeline.

## HTTP Endpoints

The service exposes operational endpoints on `ROLLING_CACHE_SINK_HTTP_PORT`:

```text
/healthz
/readyz
/metrics
```

Use these endpoints for container health checks and runtime observability rather than parsing service logs for readiness.

## Compatibility and Diagnostic Modes

`ROLLING_CACHE_PUBLICATION_METADATA_LAYOUT` also accepts `single_inode` and `metadata_only`. These layouts exist for compatibility and targeted diagnostics; the normal deployment layout is `split`.

Advanced publication controls such as commit slots and final-parent grouping are intentionally configuration-driven and disabled or conservative by default. They should be changed only together with the repository's contract and pressure tests.

## Related Components

- `services/media-worker/` — selects rolling segments and materializes evidence;
- `scripts/runtime/rolling_cache_maintenance.py` — retention and cache cleanup;
- `infra/docker-compose.midterm.yml` — service wiring;
- `infra/env/midterm.env` — deployment defaults.
