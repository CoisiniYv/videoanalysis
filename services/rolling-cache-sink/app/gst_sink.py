"""One long-lived splitmuxsink pipeline per active source session."""

from __future__ import annotations

import json
import logging
import threading
import uuid
from pathlib import Path
from typing import Any

from config import EpochResolver, SinkConfig, safe_component
from observability import SinkMetrics
from publishing import (
    SEGMENT_PUBLICATION_COMMIT_ARBITRATION_ENABLED,
    SEGMENT_PUBLICATION_OUTSTANDING_LIMIT,
    SEGMENT_PUBLICATION_PREPARE_GROUP_LIMIT,
    AtomicSegmentPublisher,
    BoundedPublicationDispatcher,
    Fragment,
    FragmentLedger,
)


LOGGER = logging.getLogger("rolling_cache_sink.gst")


class SourcePipeline:
    """Persistent H264 parser/splitmux pipeline for one source session."""

    def __init__(
        self,
        *,
        config: SinkConfig,
        metrics: SinkMetrics,
        source_id: str,
        runtime_epoch_id: str,
        session_id: str,
        frame_params: Any,
        caps_signature: str,
        gst: Any,
        build_caps: Any,
        convert_ts: Any,
        publication_dispatcher: BoundedPublicationDispatcher,
    ) -> None:
        self.source_id = safe_component(source_id, field="source_id")
        self.runtime_epoch_id = safe_component(
            runtime_epoch_id, field="runtime_epoch_id"
        )
        self.session_id = safe_component(session_id, field="session_id")
        self.caps_signature = caps_signature
        self._config = config
        self._metrics = metrics
        self._gst = gst
        self._convert_ts = convert_ts
        self._drained = threading.Event()
        self._failed = threading.Event()
        self._closed = False
        self._last_input_pts: int | None = None
        self._last_gst_pts: int | None = None
        self._last_mux_pts: int | None = None
        self._last_mux_duration = 1_000_000_000 // 24
        self._mux_timestamp_offset = 0
        self._adjust_timestamp_offset: int | None = None
        self._adjust_timestamp_mux_offset: int | None = None

        publisher = AtomicSegmentPublisher(
            cache_root=config.cache_root,
            namespace=config.namespace,
            runtime_epoch_id=self.runtime_epoch_id,
            source_id=self.source_id,
            session_id=self.session_id,
            commit_arbitration_enabled=(
                SEGMENT_PUBLICATION_COMMIT_ARBITRATION_ENABLED
            ),
        )
        self._ledger = FragmentLedger(
            publisher,
            publication_dispatcher=publication_dispatcher,
            on_published=self._on_published,
            on_publish_error=self._on_publish_error,
        )

        segment_ns = int(config.segment_seconds * 1_000_000_000)
        launch = " ! ".join(
            [
                "appsrc name=input emit-signals=false is-live=true block=true format=time",
                "queue max-size-buffers=256 max-size-bytes=0 max-size-time=0 leaky=no",
                "adjust_timestamps",
                # qtmux receives codec_data through caps. Re-injecting SPS/PPS
                # at every IDR can make h264parse emit a header buffer without
                # PTS, which is fatal to qtmux under long/high-density runs.
                "h264parse name=parser config-interval=0",
                (
                    "splitmuxsink name=segmenter "
                    f"max-size-time={segment_ns} max-size-bytes=0 max-files=0 "
                    "async-finalize=true muxer-factory=qtmux sink-factory=filesink "
                    "send-keyframe-requests=false"
                ),
            ]
        )
        self._pipeline = gst.parse_launch(launch)
        self._pipeline.set_name(f"rolling_{self.source_id}_{self.session_id}")
        self._appsrc = self._pipeline.get_by_name("input")
        self._parser = self._pipeline.get_by_name("parser")
        self._segmenter = self._pipeline.get_by_name("segmenter")
        if self._appsrc is None or self._parser is None or self._segmenter is None:
            raise RuntimeError("rolling_cache_pipeline_elements_missing")
        self._appsrc.set_caps(build_caps(frame_params))
        parser_src_pad = self._parser.get_static_pad("src")
        if parser_src_pad is None:
            raise RuntimeError("rolling_cache_parser_src_pad_missing")
        parser_src_pad.add_probe(
            self._gst.PadProbeType.BUFFER,
            self._ensure_mux_timestamp,
        )
        self._segmenter.connect("format-location-full", self._format_location)

        self._bus = self._pipeline.get_bus()
        self._bus.add_signal_watch()
        self._bus.connect("message", self._on_bus_message)
        state_result = self._pipeline.set_state(gst.State.PLAYING)
        if state_result == gst.StateChangeReturn.FAILURE:
            self._pipeline.set_state(gst.State.NULL)
            self._bus.remove_signal_watch()
            raise RuntimeError("rolling_cache_pipeline_failed_to_start")
        self._metrics.inc("pipelines_created_total")
        self._metrics.inc("source_sessions_total")
        self._metrics.inc("pipelines")
        LOGGER.info(
            "source pipeline started source=%s epoch=%s session=%s segment_s=%.3f",
            self.source_id,
            self.runtime_epoch_id,
            self.session_id,
            config.segment_seconds,
        )

    @property
    def failed(self) -> bool:
        return self._failed.is_set()

    def has_large_pts_regression(self, frame: Any) -> bool:
        if self._last_input_pts is None:
            return False
        current = self._convert_ts(frame.pts, frame.time_base)
        if current == self._gst.CLOCK_TIME_NONE:
            return False
        tolerance = int(self._config.pts_regression_tolerance_s * 1_000_000_000)
        return current + tolerance < self._last_input_pts

    def _normalize_input_timestamps(self, pts: int, dts: int) -> tuple[int, int]:
        """Clamp only the current jittered frame; never accumulate wall-clock drift."""

        if self._last_gst_pts is None or pts > self._last_gst_pts:
            return pts, dts
        correction = self._last_gst_pts + 1 - pts
        normalized_pts = pts + correction
        normalized_dts = (
            dts + correction if dts != self._gst.CLOCK_TIME_NONE else dts
        )
        self._metrics.inc("input_pts_regressions_clamped_total")
        self._metrics.inc("input_pts_clamp_ns_total", correction)
        return normalized_pts, normalized_dts

    def _next_cadence_pts(self, input_pts: int, duration: int) -> int:
        """Return a stable encoded-frame clock while preserving input PTS in JSON."""

        if self._last_gst_pts is None:
            return 0 if input_pts == self._gst.CLOCK_TIME_NONE else int(input_pts)
        step = duration if duration > 0 else self._last_mux_duration
        self._metrics.inc("mux_cadence_frames_total")
        return int(self._last_gst_pts) + max(1, int(step))

    def _ensure_mux_timestamp(self, _pad: Any, info: Any) -> Any:
        """Give parser-generated header buffers a monotonic mux timestamp."""

        buffer = info.get_buffer()
        if buffer is None:
            return self._gst.PadProbeReturn.OK
        duration = buffer.duration
        if duration != self._gst.CLOCK_TIME_NONE and duration > 0:
            self._last_mux_duration = int(duration)
        if buffer.pts == self._gst.CLOCK_TIME_NONE:
            buffer.pts = (
                self._last_mux_pts + self._last_mux_duration
                if self._last_mux_pts is not None
                else 0
            )
            if buffer.dts == self._gst.CLOCK_TIME_NONE:
                buffer.dts = buffer.pts
            self._metrics.inc("mux_pts_synthesized_total")
        else:
            buffer.pts = int(buffer.pts) + self._mux_timestamp_offset
            if buffer.dts != self._gst.CLOCK_TIME_NONE:
                buffer.dts = int(buffer.dts) + self._mux_timestamp_offset
            # Frame duration is only a fallback for parser-generated buffers
            # without timestamps.  It must not be used as the minimum spacing
            # between real timestamps: rational frame rates (for example
            # 24000/1001) legitimately alternate their rounded nanosecond
            # cadence.  Accumulating that rounding delta on every frame moves
            # splitmux boundaries away from the metadata PTS domain.
            if self._last_mux_pts is not None and buffer.pts <= self._last_mux_pts:
                correction = self._last_mux_pts + 1 - int(buffer.pts)
                buffer.pts = int(buffer.pts) + correction
                if buffer.dts != self._gst.CLOCK_TIME_NONE:
                    buffer.dts = int(buffer.dts) + correction
                self._mux_timestamp_offset += correction
                self._metrics.inc("mux_pts_regressions_corrected_total")
                self._metrics.inc("mux_pts_correction_ns_total", correction)
                if correction >= 1_000_000:
                    LOGGER.warning(
                        "corrected mux PTS regression source=%s session=%s "
                        "correction_ns=%d cumulative_offset_ns=%d",
                        self.source_id,
                        self.session_id,
                        correction,
                        self._mux_timestamp_offset,
                    )
        self._last_mux_pts = int(buffer.pts)
        return self._gst.PadProbeReturn.OK

    def write_frame(self, frame: Any, content: bytes) -> None:
        if self._closed or self.failed:
            raise RuntimeError("rolling_cache_pipeline_not_writable")
        buffer = self._gst.Buffer.new_wrapped(content)
        input_pts = self._convert_ts(frame.pts, frame.time_base)
        buffer.duration = (
            self._convert_ts(frame.duration, frame.time_base)
            if frame.duration is not None
            else self._gst.CLOCK_TIME_NONE
        )
        duration = (
            int(buffer.duration)
            if buffer.duration != self._gst.CLOCK_TIME_NONE and buffer.duration > 0
            else self._last_mux_duration
        )
        buffer.pts = self._next_cadence_pts(input_pts, duration)
        # The test source is H264 without B-frames. A stable decode timestamp
        # keeps qtmux in the same cadence domain and avoids deriving DTS from
        # bursty wall-clock arrival PTS.
        buffer.dts = buffer.pts
        if input_pts == self._gst.CLOCK_TIME_NONE:
            self._metrics.inc("input_pts_synthesized_total")
        row = json.loads(frame.json)
        if not isinstance(row, dict):
            raise ValueError("native_video_frame_json_must_be_object")
        row["rolling_cache_mux_pts"] = int(buffer.pts)
        row_id = self._ledger.queue_frame(buffer.pts, row)
        result = self._appsrc.push_buffer(buffer)
        if result != self._gst.FlowReturn.OK:
            self._ledger.discard_frame(row_id)
            raise RuntimeError(f"rolling_cache_push_failed:{result.value_nick}")
        self._last_gst_pts = buffer.pts
        if input_pts != self._gst.CLOCK_TIME_NONE:
            self._last_input_pts = int(input_pts)
        self._metrics.inc("frames_written_total")

    def finish(self, *, reason: str, actual_eos: bool) -> None:
        if self._closed:
            return
        self._closed = True
        eos_row: dict[str, Any] = {
            "source_id": self.source_id,
            "schema": "EndOfStream",
        }
        if not actual_eos:
            eos_row.update(
                {
                    "reason": reason,
                    "runtime_epoch_id": self.runtime_epoch_id,
                    "session_id": self.session_id,
                }
            )
        self._ledger.record_eos(eos_row)

        if not self.failed:
            flow = self._appsrc.end_of_stream()
            if flow != self._gst.FlowReturn.OK:
                LOGGER.error(
                    "failed to send EOS source=%s session=%s flow=%s",
                    self.source_id,
                    self.session_id,
                    flow,
                )
            if not self._drained.wait(self._config.shutdown_timeout_s):
                LOGGER.error(
                    "pipeline drain timed out source=%s session=%s pending=%d",
                    self.source_id,
                    self.session_id,
                    self._ledger.pending_count(),
                )
                self._metrics.inc("pipeline_errors_total")
        self._pipeline.set_state(self._gst.State.NULL)
        self._bus.remove_signal_watch()
        abandoned = self._ledger.abandon_pending()
        abandoned_fragments = int(abandoned.get("fragments") or 0)
        if abandoned_fragments:
            self._metrics.inc("pending_fragments", -abandoned_fragments)
            self._metrics.inc(
                "staging_fragments_abandoned_total", abandoned_fragments
            )
            self._metrics.inc(
                "staging_bytes_abandoned_total", int(abandoned.get("bytes") or 0)
            )
            LOGGER.warning(
                "discarded unpublished fragments source=%s session=%s "
                "fragments=%d bytes=%d reason=%s",
                self.source_id,
                self.session_id,
                abandoned_fragments,
                int(abandoned.get("bytes") or 0),
                reason,
            )
        if self.failed:
            self._metrics.inc("pipeline_errors_active", -1)
        self._metrics.inc("pipelines", -1)
        self._metrics.inc("pipelines_closed_total")
        LOGGER.info(
            "source pipeline closed source=%s epoch=%s session=%s reason=%s pending=%d",
            self.source_id,
            self.runtime_epoch_id,
            self.session_id,
            reason,
            self._ledger.pending_count(),
        )

    def _format_location(
        self, _splitmux: Any, fragment_id: int, first_sample: Any
    ) -> str:
        first_buffer = first_sample.get_buffer()
        if first_buffer is None or first_buffer.pts == self._gst.CLOCK_TIME_NONE:
            raise RuntimeError("splitmux_fragment_first_pts_missing")
        if self._adjust_timestamp_offset is None:
            first_input_pts = self._ledger.first_queued_gst_pts()
            if first_input_pts is None:
                raise RuntimeError("splitmux_fragment_has_no_queued_frame")
            # adjust_timestamps shifts the persistent pipeline onto a zero-based
            # running-time domain. Metadata is queued in the original Savant
            # PTS domain, so retain the session-constant offset and translate
            # every keyframe boundary back before assigning rows.
            self._adjust_timestamp_offset = first_input_pts - first_buffer.pts
            self._adjust_timestamp_mux_offset = self._mux_timestamp_offset
        # Parser-side monotonicity correction can change after the first
        # fragment.  Remove only that later delta when translating a splitmux
        # boundary back into the original Savant/input PTS domain; otherwise
        # metadata gradually migrates into neighboring fragments.
        mux_offset_delta = self._mux_timestamp_offset - int(
            self._adjust_timestamp_mux_offset or 0
        )
        input_boundary_pts = (
            first_buffer.pts + self._adjust_timestamp_offset - mux_offset_delta
        )
        location = self._ledger.open_fragment(fragment_id, input_boundary_pts)
        self._metrics.inc("pending_fragments")
        return location

    def _on_bus_message(self, _bus: Any, message: Any) -> None:
        if message.type == self._gst.MessageType.ELEMENT:
            structure = message.get_structure()
            if (
                structure is None
                or structure.get_name() != "splitmuxsink-fragment-closed"
            ):
                return
            location = structure.get_string("location")
            if not location:
                self._mark_failed("splitmux_fragment_closed_without_location")
                return
            try:
                self._ledger.close_fragment(location)
            except Exception as exc:  # callback cannot raise into GLib
                self._mark_failed(exc)
            return
        if message.type == self._gst.MessageType.ERROR:
            error, debug = message.parse_error()
            self._mark_failed(f"{error}; debug={debug}")
            self._drained.set()
            return
        if message.type == self._gst.MessageType.EOS:
            self._drained.set()

    def _on_published(self, fragment: Fragment, final_dir: Path) -> None:
        size = (final_dir / "video.mov").stat().st_size
        timings = fragment.publication_diagnostics
        self._metrics.inc("segments_published_total")
        self._metrics.inc("segment_bytes_total", size)
        self._metrics.inc("pending_fragments", -1)
        LOGGER.info(
            "segment published source=%s epoch=%s session=%s segment=%s "
            "frames=%d bytes=%d first_pts=%s last_pts=%s "
            "publish_total_ms=%s publish_stage_ms=%s publish_commit_ms=%s "
            "publish_commit_lock_wait_ms=%s "
            "publish_commit_lock_hold_ms=%s "
            "publish_validate_ms=%s "
            "publish_metadata_write_ms=%s publish_metadata_fsync_ms=%s "
            "publish_metadata_stat_ms=%s publish_manifest_write_ms=%s "
            "publish_manifest_fsync_ms=%s publish_manifest_stat_ms=%s "
            "publish_staging_dir_fsync_ms=%s publish_parent_prepare_ms=%s "
            "publish_rename_ms=%s publish_parent_dir_fsync_ms=%s "
            "publish_journal_append_ms=%s publish_accounted_ms=%s "
            "publish_unattributed_ms=%s "
            "publication_capacity_wait_ms=%s "
            "publication_queue_residence_ms=%s "
            "publication_prepare_service_ms=%s "
            "publication_commit_wait_ms=%s "
            "publication_worker_service_ms=%s "
            "publication_dispatch_total_ms=%s "
            "publication_outstanding_at_submit=%s "
            "publication_queue_depth_at_submit=%s "
            "publication_prepare_group_size=%s "
            "publication_prepare_group_position=%s",
            self.source_id,
            self.runtime_epoch_id,
            self.session_id,
            fragment.segment_id,
            sum(1 for row in fragment.rows if "pts" in row or "frame_pts" in row),
            size,
            timings.get("first_pts", "unavailable"),
            timings.get("last_pts", "unavailable"),
            timings.get("publish_total_ms", "unavailable"),
            timings.get("publish_stage_ms", "unavailable"),
            timings.get("publish_commit_ms", "unavailable"),
            timings.get("publish_commit_lock_wait_ms", "unavailable"),
            timings.get("publish_commit_lock_hold_ms", "unavailable"),
            timings.get("publish_validate_ms", "unavailable"),
            timings.get("publish_metadata_write_ms", "unavailable"),
            timings.get("publish_metadata_fsync_ms", "unavailable"),
            timings.get("publish_metadata_stat_ms", "unavailable"),
            timings.get("publish_manifest_write_ms", "unavailable"),
            timings.get("publish_manifest_fsync_ms", "unavailable"),
            timings.get("publish_manifest_stat_ms", "unavailable"),
            timings.get("publish_staging_dir_fsync_ms", "unavailable"),
            timings.get("publish_parent_prepare_ms", "unavailable"),
            timings.get("publish_rename_ms", "unavailable"),
            timings.get("publish_parent_dir_fsync_ms", "unavailable"),
            timings.get("publish_journal_append_ms", "unavailable"),
            timings.get("publish_accounted_ms", "unavailable"),
            timings.get("publish_unattributed_ms", "unavailable"),
            timings.get("publication_capacity_wait_ms", "unavailable"),
            timings.get("publication_queue_residence_ms", "unavailable"),
            timings.get("publication_prepare_service_ms", "unavailable"),
            timings.get("publication_commit_wait_ms", "unavailable"),
            timings.get("publication_worker_service_ms", "unavailable"),
            timings.get("publication_dispatch_total_ms", "unavailable"),
            timings.get("publication_outstanding_at_submit", "unavailable"),
            timings.get("publication_queue_depth_at_submit", "unavailable"),
            timings.get("publication_prepare_group_size", "unavailable"),
            timings.get("publication_prepare_group_position", "unavailable"),
        )

    def _on_publish_error(self, fragment: Fragment, error: Exception) -> None:
        self._metrics.inc("segment_publish_errors_total")
        LOGGER.error(
            "segment publication failed source=%s epoch=%s session=%s segment=%s "
            "staging=%s error=%s",
            self.source_id,
            self.runtime_epoch_id,
            self.session_id,
            fragment.segment_id,
            fragment.staging_dir,
            error,
        )
        if self._closed:
            self._metrics.inc("pipeline_errors_total")
        else:
            self._mark_failed(error)

    def _mark_failed(self, error: BaseException | str) -> None:
        if not self._failed.is_set():
            self._metrics.inc("pipeline_errors_total")
            self._metrics.inc("pipeline_errors_active")
        self._failed.set()
        LOGGER.error(
            "source pipeline error source=%s epoch=%s session=%s error=%s",
            self.source_id,
            self.runtime_epoch_id,
            self.session_id,
            error,
        )


class RollingCacheSink:
    """Dispatch Savant messages to persistent source pipelines."""

    def __init__(
        self,
        config: SinkConfig,
        epoch_resolver: EpochResolver,
        metrics: SinkMetrics,
    ) -> None:
        # Runtime imports keep pure contract tests independent of the Savant
        # image while ensuring production uses its exact 0.6.0 frame primitives.
        from gst_plugins.python.savant_rs_video_demux_common import (
            FrameParams,
            build_caps,
        )
        from savant.api.parser import convert_ts
        from savant.gstreamer import GLib, Gst
        from savant.gstreamer.codecs import Codec

        self._config = config
        self._epoch_resolver = epoch_resolver
        self._metrics = metrics
        self._frame_params_cls = FrameParams
        self._build_caps = build_caps
        self._convert_ts = convert_ts
        self._gst = Gst
        self._h264_codec = Codec.H264
        self._contexts: dict[str, SourcePipeline] = {}
        self._stopping = False
        self._publication_dispatcher = BoundedPublicationDispatcher(
            capacity=SEGMENT_PUBLICATION_OUTSTANDING_LIMIT,
            prepare_group_limit=SEGMENT_PUBLICATION_PREPARE_GROUP_LIMIT,
            metrics=metrics,
            thread_name="rolling-cache-publication",
        )
        LOGGER.info(
            "publication dispatcher started workers=1 outstanding_limit=%d "
            "prepare_group_limit=%d commit_arbitration_enabled=%s",
            SEGMENT_PUBLICATION_OUTSTANDING_LIMIT,
            SEGMENT_PUBLICATION_PREPARE_GROUP_LIMIT,
            SEGMENT_PUBLICATION_COMMIT_ARBITRATION_ENABLED,
        )

        Gst.init(None)
        self._main_loop = GLib.MainLoop()
        self._main_loop_thread = threading.Thread(
            target=self._main_loop.run,
            name="rolling-cache-gstreamer",
            daemon=True,
        )
        self._main_loop_thread.start()

    def write(self, zmq_message: Any) -> bool:
        message = zmq_message.message
        message.validate_seq_id()
        if message.is_video_frame():
            return self._write_video_frame(
                message.as_video_frame(), zmq_message.content
            )
        if message.is_end_of_stream():
            return self._write_eos(message.as_end_of_stream())
        return False

    def _write_video_frame(self, frame: Any, content: bytes | None) -> bool:
        self._metrics.inc("frames_received_total")
        source_id = safe_component(frame.source_id, field="source_id")
        params = self._frame_params_cls.from_video_frame(frame)
        if params.codec != self._h264_codec:
            self._metrics.inc("unsupported_codec_frames_total")
            LOGGER.error(
                "unsupported rolling-cache codec source=%s codec=%s",
                source_id,
                params.codec,
            )
            return False
        if not content:
            self._metrics.inc("frames_dropped_no_content_total")
            return False

        epoch = self._epoch_resolver.current()
        caps_signature = self._build_caps(params).to_string()
        context = self._contexts.get(source_id)
        rotation_reason = ""
        if context is not None:
            if context.failed:
                rotation_reason = "pipeline_error"
            elif context.runtime_epoch_id != epoch:
                rotation_reason = "runtime_epoch_changed"
                self._metrics.inc("epoch_rotations_total")
            elif context.caps_signature != caps_signature:
                rotation_reason = "caps_changed"
                self._metrics.inc("caps_rotations_total")
            elif context.has_large_pts_regression(frame):
                rotation_reason = "pts_regression"
                self._metrics.inc("pts_rotations_total")
        if rotation_reason:
            self._close_source(source_id, reason=rotation_reason, actual_eos=False)
            context = None

        if context is None:
            if not bool(frame.keyframe):
                self._metrics.inc("frames_dropped_before_keyframe_total")
                return False
            session_id = f"s{uuid.uuid4().hex[:16]}"
            try:
                context = SourcePipeline(
                    config=self._config,
                    metrics=self._metrics,
                    source_id=source_id,
                    runtime_epoch_id=epoch,
                    session_id=session_id,
                    frame_params=params,
                    caps_signature=caps_signature,
                    gst=self._gst,
                    build_caps=self._build_caps,
                    convert_ts=self._convert_ts,
                    publication_dispatcher=self._publication_dispatcher,
                )
            except Exception:
                self._metrics.inc("pipeline_errors_total")
                LOGGER.exception(
                    "failed to create source pipeline source=%s epoch=%s",
                    source_id,
                    epoch,
                )
                return False
            self._contexts[source_id] = context

        try:
            context.write_frame(frame, content)
        except Exception:
            LOGGER.exception(
                "failed to write source frame source=%s pts=%s", source_id, frame.pts
            )
            self._close_source(source_id, reason="frame_write_failed", actual_eos=False)
            return False
        return True

    def _write_eos(self, eos: Any) -> bool:
        source_id = safe_component(eos.source_id, field="source_id")
        self._metrics.inc("source_eos_total")
        return self._close_source(source_id, reason="source_eos", actual_eos=True)

    def _close_source(self, source_id: str, *, reason: str, actual_eos: bool) -> bool:
        context = self._contexts.pop(source_id, None)
        if context is None:
            return False
        context.finish(reason=reason, actual_eos=actual_eos)
        return True

    def terminate(self) -> None:
        if self._stopping:
            return
        self._stopping = True
        for source_id in list(self._contexts):
            self._close_source(source_id, reason="adapter_shutdown", actual_eos=False)
        publication_drained = self._publication_dispatcher.close(
            timeout_s=self._config.shutdown_timeout_s
        )
        publication_state = self._publication_dispatcher.snapshot()
        publication_peak = self._publication_dispatcher.peak_snapshot()
        publication_log = LOGGER.info if publication_drained else LOGGER.error
        publication_log(
            "publication dispatcher stopped drained=%s "
            "publication_capacity=%d publication_worker_count=%d "
            "publication_queue_depth=%d publication_queue_depth_peak=%d "
            "publication_outstanding=%d publication_outstanding_peak=%d "
            "publication_active=%d publication_submitted_total=%d "
            "publication_completed_total=%d publication_failed_total=%d "
            "publication_queue_wait_ms_total=%.3f "
            "publication_queue_wait_ms_max=%.3f "
            "publication_queue_wait_events_total=%d "
            "publication_prepare_group_limit=%d "
            "publication_prepare_group_total=%d "
            "publication_prepare_group_size_max=%d "
            "publication_prepare_service_ms_total=%.3f "
            "publication_prepare_service_ms_max=%.3f "
            "publication_commit_wait_ms_total=%.3f "
            "publication_commit_wait_ms_max=%.3f "
            "publication_commit_lock_wait_ms_total=%.3f "
            "publication_commit_lock_wait_ms_max=%.3f "
            "publication_commit_lock_wait_events_total=%d "
            "publication_commit_lock_hold_ms_total=%.3f "
            "publication_commit_lock_hold_ms_max=%.3f "
            "publication_queue_residence_ms_total=%.3f "
            "publication_queue_residence_ms_max=%.3f "
            "publication_queue_residence_events_total=%d "
            "publication_worker_service_ms_total=%.3f "
            "publication_worker_service_ms_max=%.3f "
            "publication_dispatch_total_ms_total=%.3f "
            "publication_dispatch_total_ms_max=%.3f "
            "publication_outstanding_peak_at_epoch_ms=%d "
            "publication_outstanding_peak_source=%s "
            "publication_outstanding_peak_segment=%s "
            "publication_shutdown_timeout_total=%d",
            publication_drained,
            int(publication_state["capacity"]),
            int(publication_state["worker_count"]),
            int(publication_state["queue_depth"]),
            int(publication_state["queue_depth_peak"]),
            int(publication_state["outstanding"]),
            int(publication_state["outstanding_peak"]),
            int(publication_state["active"]),
            int(publication_state["submitted_total"]),
            int(publication_state["completed_total"]),
            int(publication_state["failed_total"]),
            float(publication_state["queue_wait_ms_total"]),
            float(publication_state["queue_wait_ms_max"]),
            int(publication_state["queue_wait_events_total"]),
            int(publication_state["prepare_group_limit"]),
            int(publication_state["prepare_group_total"]),
            int(publication_state["prepare_group_size_max"]),
            float(publication_state["prepare_service_ms_total"]),
            float(publication_state["prepare_service_ms_max"]),
            float(publication_state["commit_wait_ms_total"]),
            float(publication_state["commit_wait_ms_max"]),
            float(publication_state["commit_lock_wait_ms_total"]),
            float(publication_state["commit_lock_wait_ms_max"]),
            int(publication_state["commit_lock_wait_events_total"]),
            float(publication_state["commit_lock_hold_ms_total"]),
            float(publication_state["commit_lock_hold_ms_max"]),
            float(publication_state["queue_residence_ms_total"]),
            float(publication_state["queue_residence_ms_max"]),
            int(publication_state["queue_residence_events_total"]),
            float(publication_state["worker_service_ms_total"]),
            float(publication_state["worker_service_ms_max"]),
            float(publication_state["dispatch_total_ms_total"]),
            float(publication_state["dispatch_total_ms_max"]),
            int(publication_peak["at_epoch_ms"]),
            str(publication_peak["source_id"]),
            str(publication_peak["segment_id"]),
            int(publication_state["shutdown_timeout_total"]),
        )
        self._main_loop.quit()
        self._main_loop_thread.join(self._config.shutdown_timeout_s)
        if self._main_loop_thread.is_alive():
            LOGGER.error("global GStreamer main loop did not stop before timeout")
