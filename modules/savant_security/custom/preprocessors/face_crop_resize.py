"""Diagnostic face crop+resize preprocessor without landmark alignment."""

from __future__ import annotations

import cv2

from savant.base.input_preproc import BasePreprocessObjectImage
from savant.meta.object import ObjectMeta
from savant.utils.image import GPUImage


class FaceCropResizePreprocessingObjectImageGPU(BasePreprocessObjectImage):
    """Crop the detected face bbox and resize it to the AdaFace input shape.

    This is a performance diagnostic used to isolate landmark warp cost. It is
    not a production-quality replacement for aligned face preprocessing.
    """

    def __call__(
        self,
        object_meta: ObjectMeta,
        frame_image: GPUImage,
        cuda_stream: cv2.cuda.Stream,
    ) -> GPUImage:
        face_img, _ = frame_image.cut(object_meta.bbox)
        return face_img.resize(resolution=(112, 112))
