#!/usr/bin/env python3
"""Register an external submitted image into person_gallery_embeddings."""

from __future__ import annotations

import argparse
import json
import sys

from app.image_face_registration import (
    MODE_EXTERNAL_IMAGE,
    STATUS_REGISTERED,
    RegistrationRequest,
    register_external_image,
)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Register an external submitted image as a gallery embedding.",
    )
    parser.add_argument("--image", required=True, help="Absolute or relative image path")
    parser.add_argument("--external-person-id", help="External person identifier")
    parser.add_argument("--name", help="Display name for new person creation")
    parser.add_argument("--person-id", type=int, help="Existing person_id to reuse")
    parser.add_argument("--description", default=None, help="Person description")
    parser.add_argument(
        "--source-type",
        default="manual_upload",
        choices=["manual_upload"],
        help="Gallery source_type. Only manual_upload is supported.",
    )
    parser.add_argument(
        "--is-primary",
        action="store_true",
        default=False,
        help="Mark the new gallery embedding as active primary.",
    )
    parser.add_argument(
        "--quality-threshold",
        type=float,
        default=0.65,
        help="Minimum accepted quality score.",
    )
    parser.add_argument(
        "--allow-multiple-faces",
        action="store_true",
        default=False,
        help="Allow multiple detected faces and select the best-quality candidate.",
    )
    parser.add_argument(
        "--keep-crop",
        action="store_true",
        default=False,
        help="Persist a registered crop artifact under FACE_REGISTRATION_ROOT.",
    )
    parser.add_argument(
        "--output-json",
        action="store_true",
        default=False,
        help="Print JSON instead of human-readable text.",
    )
    parser.add_argument(
        "--created-by",
        default="register_face_image.py",
        help="created_by / updated_by value when a person is created.",
    )
    parser.add_argument(
        "--dev-mock-embedding-fixture",
        default=None,
        help="Dev-only JSON fixture for contract/local dry-runs. Never used by default.",
    )
    parser.add_argument(
        "--face-detector-onnx",
        default=None,
        help="Path to YOLOv8-Face ONNX model. Default: YOLOV8_FACE_ONNX env or /data/video-analytics/models/yolov8_face.onnx",
    )
    parser.add_argument(
        "--adaface-onnx",
        default=None,
        help="Path to AdaFace ONNX model. Default: ADAFACE_ONNX env or /data/video-analytics/models/adaface/adaface_ir50_webface4m.onnx",
    )
    parser.add_argument(
        "--onnx-provider",
        default=None,
        help="ONNX Runtime provider spec. Default: CUDAExecutionProvider,CPUExecutionProvider",
    )
    return parser.parse_args()


def _print_text(result: dict) -> None:
    print("=== External Face Registration Result ===")
    print(f"Status: {result['status']}")
    print(f"Mode: {result['mode']}")
    if result.get("error_code"):
        print(f"Error: {result['error_code']}")
        if result.get("error_message"):
            print(f"Message: {result['error_message']}")
    print("")
    print("Person:")
    print(f"  person_id: {result.get('person_id')}")
    print(f"  person_reused: {result.get('person_reused')}")
    print(f"  external_person_id: {result.get('external_person_id')}")
    print(f"  name: {result.get('name')}")
    print("")
    print("Gallery:")
    print(f"  gallery_embedding_id: {result.get('gallery_embedding_id')}")
    print(f"  is_primary: {result.get('is_primary')}")
    print(f"  source_type: {result.get('source_type')}")
    print(f"  embedding_model: {result.get('embedding_model')}")
    print(f"  model_version: {result.get('model_version')}")
    print(f"  embedding_dim: {result.get('embedding_dim')}")
    print(f"  embedding_norm: {result.get('embedding_norm')}")
    print(f"  quality: {result.get('quality')}")
    print("")
    print("Image:")
    print(f"  source_image_path: {result.get('source_image_path')}")
    print(f"  registered_crop_path: {result.get('registered_crop_path')}")
    print(f"  face_bbox: {result.get('face_bbox')}")
    print(f"  landmarks: {result.get('landmarks')}")
    print("")
    print("Flags:")
    print(f"  storage_fallback_used: {result.get('storage_fallback_used')}")
    print(f"  real_embedding_used: {result.get('real_embedding_used')}")
    print(f"  dev_mock_used: {result.get('dev_mock_used')}")
    print(f"  fallback_used: {result.get('fallback_used')}")
    if result.get("detector_providers"):
        print(f"  detector_providers: {result['detector_providers']}")
    if result.get("embedder_providers"):
        print(f"  embedder_providers: {result['embedder_providers']}")
    print("")
    if result["status"] == STATUS_REGISTERED and result["mode"] == MODE_EXTERNAL_IMAGE:
        print("Result:")
        print("  PASS: external submitted image registered successfully")
    else:
        print("Result:")
        print("  FAIL: external submitted image registration did not complete")


def main() -> None:
    args = _parse_args()
    request = RegistrationRequest(
        image_path=args.image,
        external_person_id=args.external_person_id,
        name=args.name,
        person_id=args.person_id,
        description=args.description,
        source_type=args.source_type,
        is_primary=args.is_primary,
        quality_threshold=args.quality_threshold,
        allow_multiple_faces=args.allow_multiple_faces,
        keep_crop=args.keep_crop,
        created_by=args.created_by,
        dev_mock_embedding_fixture=args.dev_mock_embedding_fixture,
        face_detector_onnx=args.face_detector_onnx,
        adaface_onnx=args.adaface_onnx,
        onnx_provider=args.onnx_provider,
    )
    result = register_external_image(request).to_dict()
    if args.output_json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        _print_text(result)
    sys.exit(0 if result["status"] == STATUS_REGISTERED else 1)


if __name__ == "__main__":
    main()
