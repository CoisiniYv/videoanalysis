"""Contain invalid decoder output before it reaches native stage-move probes.

The Rust C API used by the native probe aborts the process on a backward stage
move. The Python binding returns an exception instead. Deduplication also covers
two outputs with the same ID while the first is still at source-convert (an equal
stage move is otherwise legal). This protects identity; it does not repair a
damaged compressed stream or recover events from rejected frames.
"""

from collections import OrderedDict
from threading import Lock


class DecodedFrameGuard:
    """One guard per decoder output pad, with bounded identity history."""

    def __init__(self, pipeline, *, on_drop=None, history_size=1024):
        if history_size < 1:
            raise ValueError('history_size must be positive')
        self._pipeline = pipeline
        self._on_drop = on_drop
        self._history_size = history_size
        self._recent = OrderedDict()
        self._lock = Lock()

    @property
    def recent_ids(self):
        with self._lock:
            return tuple(self._recent)

    def _reject(self, **diagnostic):
        if self._on_drop is not None:
            try:
                self._on_drop(**diagnostic)
            except Exception:
                # An observer must never turn a rejection into admission.
                pass
        return False

    def admit(self, frame_id, *, source_id, buffer_pts=None):
        diagnostic = dict(source_id=source_id, frame_id=frame_id, buffer_pts=buffer_pts)
        with self._lock:
            if not isinstance(frame_id, int) or isinstance(frame_id, bool) or frame_id < 0:
                diagnostic['reason'] = 'missing_metadata'
            elif frame_id in self._recent:
                diagnostic.update(reason='duplicate', frame_uuid=self._recent[frame_id])
            else:
                try:
                    frame, _ = self._pipeline.get_independent_frame(frame_id)
                    diagnostic.update(frame_uuid=str(frame.uuid), frame_pts=frame.pts)
                except Exception as exc:
                    diagnostic.update(reason='frame_unavailable', error=str(exc))
                else:
                    if frame.source_id != source_id:
                        diagnostic.update(reason='source_mismatch', actual_source_id=frame.source_id)
                    else:
                        try:
                            self._pipeline.move_as_is('source-convert', [frame_id])
                        except Exception as exc:
                            diagnostic.update(reason='stage_rejected', error=str(exc))
                        else:
                            self._recent[frame_id] = diagnostic['frame_uuid']
                            if len(self._recent) > self._history_size:
                                self._recent.popitem(last=False)
                            return True
        # Never delete the pipeline frame here: its original buffer may still
        # own it downstream. Only the rejected Gst.Buffer is dropped by the probe.
        return self._reject(**diagnostic)


def add_decoded_frame_guard(pad, pipeline, source_id, logger):
    """Replace, rather than precede, the aborting source-convert C API probe."""
    from pygstsavantframemeta import gst_buffer_get_savant_frame_meta
    from savant.gstreamer import Gst
    from savant.metrics import get_or_create_counter

    accepted = get_or_create_counter(
        'decoded_frame_guard_accepted_total',
        'Decoded buffers admitted to source-convert with valid unique identity.',
        label_names=['source_id'],
    )
    dropped = get_or_create_counter(
        'decoded_frame_guard_dropped_total',
        'Decoded buffers rejected before source-convert, by reason.',
        label_names=['source_id', 'reason'],
    )
    counts = {}

    def count(counter, labels):
        try:
            counter.inc(1, labels)
        except Exception:
            logger.exception('[decoded-frame-guard] Counter update failed')

    def on_drop(**row):
        reason = row['reason']
        count(dropped, [source_id, reason])
        counts[reason] = counts.get(reason, 0) + 1
        n = counts[reason]
        # Keep startup examples and exponentially spaced diagnostics. Counters
        # retain every rejection without flooding a busy streaming thread.
        if n <= 3 or n & (n - 1) == 0:
            logger.warning('[decoded-frame-guard] drop_count=%s %s', n, row)

    guard = DecodedFrameGuard(pipeline, on_drop=on_drop)

    def probe(_pad, info):
        try:
            buffer = info.get_buffer()
            meta = gst_buffer_get_savant_frame_meta(buffer) if buffer is not None else None
            admitted = guard.admit(
                meta.idx if meta is not None else None,
                source_id=source_id,
                buffer_pts=buffer.pts if buffer is not None else None,
            )
        except Exception as exc:
            on_drop(reason='probe_error', source_id=source_id, error=str(exc))
            return Gst.PadProbeReturn.DROP
        if not admitted:
            return Gst.PadProbeReturn.DROP
        count(accepted, [source_id])
        return Gst.PadProbeReturn.OK

    logger.info('[decoded-frame-guard] enabled source_id=%s', source_id)
    return pad.add_probe(Gst.PadProbeType.BUFFER, probe)
