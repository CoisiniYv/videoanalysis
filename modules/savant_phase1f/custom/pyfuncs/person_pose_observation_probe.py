"""PersonPoseObservationProbe — Phase 1F diagnostic.

Calls the ``person_pose_adapter`` to build ``PersonPoseObservation`` objects
from official Savant metadata and logs a structured summary each frame.

This probe is the successor to Phase 1E's ``MetadataHandoffProbe``.  While
Phase 1E answered *whether* metadata arrives, Phase 1F verifies *that the
adapter layer correctly transforms* metadata into domain objects.
"""

from __future__ import annotations

import logging
from typing import List

from savant.deepstream.pyfunc import NvDsPyFuncPlugin

from custom.adapters.person_pose_adapter import build_person_pose_observations
from custom.models.pose import PersonPoseObservation, is_valid_track_id

logger = logging.getLogger(__name__)

_DEFAULT_LOG_INTERVAL = 15


class PersonPoseObservationProbe(NvDsPyFuncPlugin):
    """PyFunc that logs PersonPoseObservation diagnostics.

    Summary output (every *log_every_n_frames* frames)::

        stage=phase1f_person_pose_observation
        observation_adapter_ok=true|false
        observation_count=<N>
        skipped_untracked_person_count=<N>
        skipped_no_keypoints_count=<N>
        first_valid_track_id=<id>|none
        first_keypoints_flat_len=<51|0>
        first_keypoints_len=<17|0>
        first_bbox=(x,y,w,h)
        first_confidence=<float>
        first_keypoint_confidence=<float>
    """

    def __init__(
        self,
        log_every_n_frames: int = _DEFAULT_LOG_INTERVAL,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.log_every_n_frames = int(log_every_n_frames)
        self.frame_count = 0

    def process_frame(self, buffer, frame_meta):
        self.frame_count += 1

        if self.frame_count % self.log_every_n_frames != 0:
            return

        # ---- Build PersonPoseObservation list -----------------------------
        result = build_person_pose_observations(frame_meta)
        observations = result.observations
        tracked: List[PersonPoseObservation] = [
            o for o in observations if is_valid_track_id(o.track_id)
        ]

        observation_adapter_ok = len(tracked) > 0

        parts = [
            "stage=phase1f_person_pose_observation",
            f"observation_adapter_ok={'true' if observation_adapter_ok else 'false'}",
            f"observation_count={len(observations)}",
            f"skipped_untracked_person_count={result.skipped_untracked_person_count}",
            f"skipped_no_keypoints_count={result.skipped_no_keypoints_count}",
        ]

        if tracked:
            first = tracked[0]
            parts.append(f"first_valid_track_id={first.track_id}")
            parts.append(f"first_keypoints_flat_len={len(first.keypoints) * 3}")
            parts.append(f"first_keypoints_len={len(first.keypoints)}")
            parts.append(
                f"first_bbox=({first.bbox.x:.2f},{first.bbox.y:.2f},"
                f"{first.bbox.width:.2f},{first.bbox.height:.2f})"
            )
            parts.append(f"first_confidence={first.confidence:.4f}")
            parts.append(f"first_keypoint_confidence={first.keypoint_confidence:.4f}")
        else:
            parts.append("first_valid_track_id=none")
            parts.append("first_keypoints_flat_len=0")
            parts.append("first_keypoints_len=0")
            parts.append("first_bbox=(0,0,0,0)")
            parts.append("first_confidence=0.0")
            parts.append("first_keypoint_confidence=0.0")

        print(" ".join(parts), flush=True)
