#!/usr/bin/env python3
"""Export midterm DB camera configuration into generated runtime config files."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import psycopg


SCRIPT_DIR = Path(__file__).resolve().parent
API_ROOT = SCRIPT_DIR.parent
if str(API_ROOT) not in sys.path:
    sys.path.insert(0, str(API_ROOT))

from app.repositories.cameras import CameraRepository
from app.runtime_config_export import (
    DEFAULT_OUTPUT_DIR,
    ExportOptions,
    ExportValidationError,
    export_runtime_config,
)


DEFAULT_DATABASE_URL = "postgresql://video:video@localhost:5438/video_analytics"


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        with psycopg.connect(args.database_url, autocommit=True) as conn:
            repo = CameraRepository(conn)
            result = export_runtime_config(
                repo,
                ExportOptions(
                    camera_id=args.camera_id,
                    all_enabled=args.all_enabled,
                    include_disabled=args.include_disabled,
                    output_dir=Path(args.output_dir),
                    redact_secrets=args.redact_secrets,
                    dry_run=args.dry_run,
                ),
            )
    except ExportValidationError as exc:
        print(
            json.dumps(
                {"result": "FAIL_VALIDATION", "validation_errors": exc.errors},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 2
    except Exception as exc:
        print(
            json.dumps({"result": "FAIL_EXPORT", "error": str(exc)}, ensure_ascii=False),
            file=sys.stderr,
        )
        return 1

    response = {
        "result": "DRY_RUN_OK" if result.dry_run else "PASS_MIDTERM_DB_CONFIG_EXPORT",
        "dry_run": result.dry_run,
        "paths": result.paths_dict(),
        "camera_count": result.export_summary["camera_count"],
    }
    print(json.dumps(response, ensure_ascii=False))
    return 0


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Export PostgreSQL camera config into generated runtime config files.",
    )
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--camera-id", help="Export one camera by camera id.")
    target.add_argument(
        "--all-enabled",
        action="store_true",
        help="Export all enabled cameras, or all cameras with --include-disabled.",
    )
    parser.add_argument(
        "--include-disabled",
        action="store_true",
        help="Include disabled cameras, zones, and rules.",
    )
    parser.add_argument(
        "--output-dir",
        default=DEFAULT_OUTPUT_DIR,
        help=f"Output directory (default: {DEFAULT_OUTPUT_DIR}).",
    )
    parser.add_argument(
        "--redact-secrets",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Redact RTSP credentials in summary/log output (default: true).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate and build export documents without writing final files.",
    )
    parser.add_argument(
        "--database-url",
        default=os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL),
        help="PostgreSQL connection string.",
    )
    return parser.parse_args(argv)


if __name__ == "__main__":
    sys.exit(main())
