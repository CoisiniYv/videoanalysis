"""Pure contracts for frame-indexed evidence metadata."""

from .keys import frame_pts_key, frame_uuid_key, identity_patch_key
from .merge import merge_identity_patches
from .validation import (
    validate_frame_annotation_message,
    validate_identity_patch_message,
)

__all__ = [
    "frame_pts_key",
    "frame_uuid_key",
    "identity_patch_key",
    "merge_identity_patches",
    "validate_frame_annotation_message",
    "validate_identity_patch_message",
]
