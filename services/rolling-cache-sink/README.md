# Rolling-cache sink

This replaces Savant 0.6.0 `video_files.py` for the rolling cache only. It keeps
one H264 passthrough GStreamer pipeline per active source session and rotates
MOV fragments with `splitmuxsink` at the configured time/keyframe boundary.

The build context is the repository root:

```sh
docker build -f services/rolling-cache-sink/Dockerfile .
```

Required environment: `ZMQ_ENDPOINT`. Existing rolling-cache variables remain
valid, especially `ROLLING_CACHE_ROOT`, `ROLLING_CACHE_NAMESPACE`,
`ROLLING_CACHE_SEGMENT_SECONDS`, `ROLLING_CACHE_RUNTIME_EPOCH_ID`, and
`RUNTIME_EPOCH_STATE_PATH`. `ROLLING_CACHE_SINK_HTTP_PORT` defaults to `8080`
and serves `/metrics`, `/healthz`, and `/readyz`.

Published segments retain the consumer contract:

```text
<root>/midterm/epochs/<runtime_epoch_id>/<source_id>/segments/<segment_id>/
  video.mov
  metadata.json   # native Savant JSONL; publication commit marker
  segment_manifest.json
```

The default-off `metadata_only` v3 diagnostic intentionally omits
`segment_manifest.json`; its bounded manifest control record is the first line
of `metadata.json`, followed by the exact native Savant JSONL rows.

Finalized fragments are staged outside the source directory and become visible
through one same-filesystem directory rename. Failed or interrupted fragments
remain under `.rolling-cache-staging` without a visible `metadata.json` in any
source tree; the service never deletes or overwrites them.

The production/default metadata layout is `split`: `metadata.json` and the
compact v1 manifest are separate immutable files and each receives its own
regular-file `fsync`. The default-off diagnostic
`ROLLING_CACHE_PUBLICATION_METADATA_LAYOUT=single_inode` writes a bounded v2
manifest control record followed by the native frame JSONL into one inode, then
hard-links both historical names to that inode. It performs one regular-file
sync while retaining the staging-directory fsync, atomic directory rename and
final-parent fsync. Media Worker accepts both layouts, parses only the bounded
first v2 record for discovery and excludes it from frame rows. This diagnostic
does not use filesystem-wide sync and does not change the daily `split`
default.

`ROLLING_CACHE_PUBLICATION_METADATA_LAYOUT=metadata_only` retains the embedded
representation with a v3 control record but creates no hard-link alias. It
performs one regular-file sync followed by the unchanged staging-directory
sync, atomic directory rename and final-parent sync. The publication journal
records the metadata identity as both the catalog and metadata identity; Media
Worker keys v3 catalog entries by `metadata.json` and keeps journal,
filesystem reconciliation, fallback discovery, read-pin and v1/v2
compatibility intact. This mode is also default-off; `split` remains the daily
layout.

`ROLLING_CACHE_PUBLICATION_FINAL_PARENT_GROUP_LIMIT` defaults to `1`. Values
`2..32` enable a bounded diagnostic cohort without changing the file or
staging-directory fences: each item is written and fully fsynced while still
invisible before the next item is staged, then the cohort is renamed in exact
FIFO order, every distinct final parent is explicitly fsynced, journals are
appended in FIFO order, and callbacks run only after the complete cohort
fence. This is intentionally different from the rejected multi-item
preparation experiment, which dirtied a whole group before its first file
fence. Cohort mode is incompatible with multiple publication workers,
preparation grouping, or commit slots and remains outside the daily preset
until a retained pressure artifact proves the capacity gate.
