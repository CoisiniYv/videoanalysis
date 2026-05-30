"""Compatibility shim for the renamed frame anchor metadata helper.

The production-safe implementation lives in
``custom.services.frame_anchor_metadata``. Keep this module so older imports of
``custom.services.frame_uuid_probe`` continue to resolve without duplicating
logic.
"""

from custom.services.frame_anchor_metadata import *  # noqa: F401,F403
