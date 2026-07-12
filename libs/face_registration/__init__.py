"""Shared external face registration package."""

from .image_face_registration import (
    BatchRegistrationRequest,
    BatchRegistrationResult,
    MODE_EXTERNAL_IMAGE,
    SOURCE_TYPE_MANUAL_UPLOAD,
    STATUS_FAILED,
    STATUS_PARTIAL,
    STATUS_REGISTERED,
    RegistrationRequest,
    RegistrationResult,
    register_external_image,
    register_external_images,
)

__all__ = [
    "MODE_EXTERNAL_IMAGE",
    "SOURCE_TYPE_MANUAL_UPLOAD",
    "STATUS_FAILED",
    "STATUS_PARTIAL",
    "STATUS_REGISTERED",
    "BatchRegistrationRequest",
    "BatchRegistrationResult",
    "RegistrationRequest",
    "RegistrationResult",
    "register_external_image",
    "register_external_images",
]
