# Savant v0.6.0 overlay patches (source reset and decoder identity)

## What breaks upstream

Image `ghcr.io/insight-platform/savant-deepstream:0.6.0-7.1`, framework tag
`v0.6.0` (the race is still present on upstream `develop` as of 2026-04).

Failure chain observed in production (2 RTSP streams):

1. A source delivers a non-monotonous PTS (RTSP reconnect, adapter restart,
   or a looping movie stream such as `primary_rtsp`'s `1080movie`, which
   resets PTS on every loop).
2. `savant_rs_video_decode_bin.py:163` hard-codes
   `eos-on-timestamps-reset=True` (not configurable in v0.6.0), so
   `savant_rs_video_demux.check_timestamps()` runs
   `remove_source(send_eos=True)`.
3. On EOS, `pipeline._remove_output_elements()` calls
   `self._sources.remove_source()` and releases the `SourceInfo` registry
   entry, while stale buffers of that source are still queued in the
   muxer / nvinfer stages.
4. The stale buffers reach unguarded `self._sources.get_source(...)` calls
   and raise `KeyError: 'source_...'`. The buffer-probe wrapper
   (`savant/gstreamer/utils.py:_buffer_probe_callback`) converts this into a
   GST bus error -> `ModuleStatus.STOPPING` -> `STOPPED`.
5. The entrypoint's shutdown path can hang (upstream TODO in
   `savant/entrypoint/main.py`), so the python process never exits, the
   container stays `Up (unhealthy)`, and `restart: unless-stopped` never
   fires. Replay then times out sending to Savant and the source adapters
   loop-restart.

Two parallel streams do not cause this; they only widen the race window
(more in-flight buffers per source, near-simultaneous resets).

## What the patches change

KeyError -> warning log + skip the zombie frame at four registry call sites:

| file | site | behaviour on missing source |
|---|---|---|
| `buffer_processor.py` | `_prepare_input_frame` (v0.6.0 L150) | skip frame |
| `nvinfer_processor.py` -> `nvinfer/processor.py` | `_process_custom_model_output` (L271) | skip frame, continue batch |
| `pipeline.py` | `_update_meta_for_single_frame` (L952) | skip frame |
| `pipeline.py` | `_on_muxer_sink_pad_peer_eos` (L1154) | ignore late/duplicate EOS |

Patched log lines carry the `[video-analytics patch]` marker for grepping.

The decoder output also uses `decoded_frame_guard.py` before conversion. In the
2026-10-11 pressure test, a buffer carrying an ID already at `muxer` reached the
native `source-convert` probe. Its Rust C API panicked and aborted Savant. This
overlay replaces that probe with the exception-returning Python `move_as_is`
binding, verifies the frame's source, and rejects repeated IDs with a locked,
bounded per-pad history (1,024 entries). Unique reordered IDs remain valid.
Evicted IDs still undergo the safe native stage check. The guard never changes
UUID/PTS or deletes an original frame that may still be in flight.

Metrics `decoded_frame_guard_accepted_total` and
`decoded_frame_guard_dropped_total` (source ID and rejection reason) expose the
cost. Detailed `[decoded-frame-guard]` logs retain initial and exponentially
spaced examples. Rejected frames can lose detections. This is crash containment;
it does not establish why a decoder emitted the duplicate, repair corrupted
compressed input, or certify evidence quality. Full decoding and the 180-second
deadline must still be validated in the pressure test.

## How they are applied

`apply_patches.py` runs from the savant container entrypoint before
`python -m savant.entrypoint` (see `infra/docker-compose.midterm.yml`). It
locates the installed `savant` package without importing it, verifies the
target file md5 against the pristine v0.6.0 or known previous overlay baseline, copies the patched
file over it and drops stale `__pycache__` entries. Idempotent across
restarts (the savant container filesystem layer persists between restarts;
after `docker compose up --force-recreate` it simply re-applies).

Baselines (pristine v0.6.0 -> patched):

```
deepstream/buffer_processor.py   ab13b915a8a7fc7acd6265d054054036 -> 556af89b356401efa1dc9d5c2c4d3c68
deepstream/nvinfer/processor.py  e6a05fb0e0eb7dd04c9d01e4fc2bb80f -> 9615f7cd134f623a3950b65e5ad71fb1
deepstream/pipeline.py           5c418afd69e9d478a43cc1a123513837 -> 524b53c51e959387ebe7034ab7754433
deepstream/decoded_frame_guard.py  (new helper) -> 25fd6e01bb780176a3ac8613f82a6129
```

The previous pipeline overlay `d2a5581899765f173fdfc6e2f1515b2d` is accepted for
upgrade. All files, including the helper, are checked before any write. A missing
or modified helper prevents installing a pipeline that would import it.

If the installed file matches neither md5 (different image build), the
script refuses to patch and aborts the start (`SAVANT_PATCH_ENFORCE=true` by
default) so a mismatched framework is never silently modified. Set
`SAVANT_PATCH_ENFORCE=false` to boot unpatched instead, or
`SAVANT_PATCH_ENABLED=false` to disable patching entirely.

## Verifying after deploy

```bash
docker logs video-analytics-midterm-savant 2>&1 | grep savant_patches
# expect: known checksums accepted and all four files installed (or already patched)

docker exec video-analytics-midterm-savant python - <<'EOF'
import hashlib, savant.deepstream.buffer_processor as m
print(m.__file__, hashlib.md5(open(m.__file__, 'rb').read()).hexdigest())
EOF
# expect md5 556af89b356401efa1dc9d5c2c4d3c68
```

During a PTS reset you should now see warnings with
`[video-analytics patch]` instead of `KeyError` + module stop.

Run `python scripts/tools/check_decoded_frame_guard.py` in the pinned Savant
image with the repository mounted to verify real GStreamer metadata and pad
callbacks against `savant_rs`. It needs no GPU: duplicate and backward-stage
buffers must be dropped, a subsequent valid buffer must pass, and the original
UUID must be unchanged. Unit and overlay-installation tests live in
`harness/tests/test_decoded_frame_guard.py` and `test_savant_patch_loader.py`.

## Rollback

Set `SAVANT_PATCH_ENABLED=false` on the savant-security service and
recreate the container (`docker compose up -d --force-recreate
savant-security`); recreation restores the image's pristine files.

## If the image is upgraded

These overlays are pinned to v0.6.0. On any image upgrade, re-extract the
new framework files, re-apply the four guards, and refresh the md5 table in
`apply_patches.py` (the enforce check will loudly fail otherwise, which is
intentional).
