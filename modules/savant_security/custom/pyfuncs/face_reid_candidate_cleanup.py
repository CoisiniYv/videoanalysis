"""Remove temporary pre-gated AdaFace candidate objects after export."""

from __future__ import annotations

from typing import Any

from savant.deepstream.pyfunc import NvDsPyFuncPlugin


class FaceReidCandidateCleanupPyFunc(NvDsPyFuncPlugin):
    """Keep temporary embedding candidates out of annotations and sink metadata."""

    def __init__(
        self,
        candidate_element_name: str = "face_reid_candidate",
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self._candidate_element_name = str(candidate_element_name)

    def process_frame(self, buffer: Any, frame_meta: Any) -> None:
        candidates = [
            obj
            for obj in list(frame_meta.objects)
            if getattr(obj, "element_name", "") == self._candidate_element_name
        ]
        for obj in candidates:
            frame_meta.remove_obj_meta(obj)
