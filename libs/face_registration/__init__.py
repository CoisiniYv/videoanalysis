"""Shared external face registration package."""

from .image_face_registration import (
    MODE_EXTERNAL_IMAGE,
    SOURCE_TYPE_MANUAL_UPLOAD,
    STATUS_FAILED,
    STATUS_REGISTERED,
    RegistrationRequest,
    RegistrationResult,
    register_external_image,
)

__all__ = [
    "MODE_EXTERNAL_IMAGE",
    "SOURCE_TYPE_MANUAL_UPLOAD",
    "STATUS_FAILED",
    "STATUS_REGISTERED",
    "RegistrationRequest",
    "RegistrationResult",
    "register_external_image",
]
