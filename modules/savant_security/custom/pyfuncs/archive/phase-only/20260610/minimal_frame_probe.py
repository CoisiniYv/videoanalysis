import logging

from savant.deepstream.pyfunc import NvDsPyFuncPlugin

logger = logging.getLogger(__name__)


class MinimalFrameProbe(NvDsPyFuncPlugin):
    """Phase 1C minimal frame probe.

    This PyFunc proves that frames flow through the Savant DeepStream pipeline.
    It does not run inference, tracking, rules, Redis output, or database writes.
    """

    def __init__(
        self,
        source_id: str = "phase1c.local.test_video",
        camera_id: str = "phase1c_cam_001",
        log_every_n_frames: int = 30,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.expected_source_id = source_id
        self.camera_id = camera_id
        self.log_every_n_frames = int(log_every_n_frames)
        self.frame_count = 0

    def on_start(self) -> bool:
        self.logger.info(
            "MinimalFrameProbe started | expected_source=%s camera=%s log_every=%s",
            self.expected_source_id,
            self.camera_id,
            self.log_every_n_frames,
        )
        return super().on_start()

    def process_frame(self, buffer, frame_meta):
        self.frame_count += 1

        if self.frame_count % self.log_every_n_frames != 0:
            return

        source_id = getattr(frame_meta, "source_id", self.expected_source_id)
        frame_num = getattr(frame_meta, "frame_num", self.frame_count)
        pts = getattr(frame_meta, "pts", None)
        framerate = getattr(frame_meta, "framerate", None)

        self.logger.info(
            "Phase1C Frame #%s | source=%s camera=%s frame_num=%s pts=%s framerate=%s stage=phase1c_frame_probe",
            self.frame_count,
            source_id,
            self.camera_id,
            frame_num,
            pts,
            framerate,
        )
