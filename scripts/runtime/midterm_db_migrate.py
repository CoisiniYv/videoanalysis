#!/usr/bin/env python3
"""Run ordered PostgreSQL migrations with an auditable backup and session lock.

The runner intentionally uses one session for the advisory lock and invokes
psql once per migration. This keeps migrations such as CREATE INDEX
CONCURRENTLY outside an implicit transaction while preventing two deployers
from applying the same revision at the same time.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from zoneinfo import ZoneInfo

import psycopg
from psycopg.rows import dict_row


MIGRATION_TABLE = "_video_analytics_schema_migrations"
LOCK_KEY = "video-analytics:midterm:migrations"
VERSION_RE = re.compile(r"^(?P<version>\d+)_.*\.sql$")
SEOUL = ZoneInfo("Asia/Seoul")


@dataclass(frozen=True)
class Migration:
    version: int
    filename: str
    path: Path
    sha256: str


def now_seoul() -> str:
    return datetime.now(SEOUL).isoformat()


def redact_database_url(value: str) -> str:
    parsed = urlsplit(value)
    if not parsed.scheme or not parsed.netloc:
        return "<invalid-or-unparseable>"
    host = parsed.hostname or ""
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    port = f":{parsed.port}" if parsed.port else ""
    return urlunsplit((parsed.scheme, f"{host}{port}", parsed.path, "", ""))


def database_url(args: argparse.Namespace) -> str:
    value = args.database_url or os.getenv("VIDEO_ANALYTICS_DATABASE_URL") or os.getenv("DATABASE_URL")
    if not value:
        raise SystemExit("database URL is required via --database-url, VIDEO_ANALYTICS_DATABASE_URL, or DATABASE_URL")
    return value


def discover_migrations(directory: Path) -> list[Migration]:
    migrations: list[Migration] = []
    for path in sorted(directory.glob("*.sql")):
        match = VERSION_RE.match(path.name)
        if not match:
            continue
        migrations.append(
            Migration(
                version=int(match.group("version")),
                filename=path.name,
                path=path,
                sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
            )
        )
    versions = [item.version for item in migrations]
    if len(versions) != len(set(versions)):
        raise SystemExit("duplicate migration version detected")
    return sorted(migrations, key=lambda item: item.version)


def ensure_migration_table(conn: psycopg.Connection) -> None:
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {MIGRATION_TABLE} (
            version INTEGER PRIMARY KEY,
            filename TEXT NOT NULL,
            sha256 TEXT NOT NULL,
            applied_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            duration_ms INTEGER NOT NULL DEFAULT 0
        )
        """
    )


def acquire_lock(conn: psycopg.Connection, timeout_s: int) -> None:
    deadline = time.monotonic() + max(1, timeout_s)
    while True:
        row = conn.execute(
            "SELECT pg_try_advisory_lock(hashtext(%s)) AS acquired",
            (LOCK_KEY,),
        ).fetchone()
        if row and bool(row["acquired"]):
            return
        if time.monotonic() >= deadline:
            raise RuntimeError(f"migration advisory lock timeout after {timeout_s}s")
        time.sleep(1)


def run_psql(database: str, migration: Migration) -> None:
    completed = subprocess.run(
        ["psql", "--dbname", database, "--set", "ON_ERROR_STOP=1", "--file", str(migration.path)],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    if completed.returncode != 0:
        output = (completed.stdout or "").strip()[-4000:]
        raise RuntimeError(f"migration {migration.filename} failed (rc={completed.returncode})\n{output}")


def run_backup(database: str, backup_dir: Path) -> dict[str, str]:
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(SEOUL).strftime("%Y%m%dT%H%M%S%z")
    dump_path = backup_dir / f"video-analytics-{stamp}.dump"
    metadata_path = dump_path.with_suffix(".json")
    completed = subprocess.run(
        ["pg_dump", "--format=custom", "--no-owner", "--file", str(dump_path), "--dbname", database],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"pg_dump failed (rc={completed.returncode})\n{(completed.stdout or '').strip()[-4000:]}"
        )
    sha256 = hashlib.sha256(dump_path.read_bytes()).hexdigest()
    metadata = {
        "schema_version": "video-analytics.database-backup.v1",
        "created_at": now_seoul(),
        "database": redact_database_url(database),
        "file": str(dump_path),
        "sha256": sha256,
    }
    metadata_path.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    return metadata


def applied_rows(conn: psycopg.Connection) -> dict[int, dict[str, object]]:
    rows = conn.execute(
        f"SELECT version, filename, sha256, applied_at, duration_ms FROM {MIGRATION_TABLE} ORDER BY version"
    ).fetchall()
    return {int(row["version"]): dict(row) for row in rows}


def migration_status(database: str, migration_dir: Path) -> dict[str, object]:
    migrations = discover_migrations(migration_dir)
    with psycopg.connect(database, autocommit=True, row_factory=dict_row) as conn:
        try:
            applied = applied_rows(conn)
        except psycopg.errors.UndefinedTable:
            # A fresh database has no migration ledger yet; status remains
            # read-only and reports every discovered migration as pending.
            applied = {}
    pending = []
    checksum_mismatches = []
    for migration in migrations:
        row = applied.get(migration.version)
        if row is None:
            pending.append(migration.filename)
        elif str(row["sha256"]) != migration.sha256:
            checksum_mismatches.append(migration.filename)
    return {
        "schema_version": "video-analytics.migration-status.v1",
        "generated_at": now_seoul(),
        "database": redact_database_url(database),
        "migration_dir": str(migration_dir),
        "applied_versions": sorted(applied),
        "pending": pending,
        "checksum_mismatches": checksum_mismatches,
        "ok": not checksum_mismatches,
    }


def apply_migrations(database: str, migration_dir: Path, backup_dir: Path | None, lock_timeout_s: int) -> dict[str, object]:
    migrations = discover_migrations(migration_dir)
    backup = None
    applied_now: list[str] = []
    with psycopg.connect(database, autocommit=True, row_factory=dict_row) as conn:
        ensure_migration_table(conn)
        acquire_lock(conn, lock_timeout_s)
        try:
            # Hold the same deployment lock while taking the backup so another
            # migration cannot change the schema between the dump and apply.
            if backup_dir is not None:
                backup = run_backup(database, backup_dir)
            applied = applied_rows(conn)
            for migration in migrations:
                existing = applied.get(migration.version)
                if existing is not None:
                    if str(existing["filename"]) != migration.filename or str(existing["sha256"]) != migration.sha256:
                        raise RuntimeError(
                            f"migration checksum mismatch for {migration.filename}; refusing to continue"
                        )
                    continue
                started = time.monotonic()
                run_psql(database, migration)
                duration_ms = int((time.monotonic() - started) * 1000)
                conn.execute(
                    f"INSERT INTO {MIGRATION_TABLE} (version, filename, sha256, duration_ms) VALUES (%s, %s, %s, %s)",
                    (migration.version, migration.filename, migration.sha256, duration_ms),
                )
                applied_now.append(migration.filename)
        finally:
            conn.execute("SELECT pg_advisory_unlock(hashtext(%s))", (LOCK_KEY,))
    return {
        "schema_version": "video-analytics.migration-run.v1",
        "completed_at": now_seoul(),
        "database": redact_database_url(database),
        "migration_dir": str(migration_dir),
        "applied": applied_now,
        "backup": backup,
        "ok": True,
    }


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("status", "backup", "migrate"))
    parser.add_argument("--database-url")
    parser.add_argument("--migration-dir", type=Path, default=Path(__file__).resolve().parents[2] / "db" / "migrations")
    parser.add_argument("--backup-dir", type=Path)
    parser.add_argument("--lock-timeout-s", type=int, default=300)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    database = database_url(args)
    if args.command == "backup":
        if args.backup_dir is None:
            raise SystemExit("backup requires --backup-dir")
        print(json.dumps(run_backup(database, args.backup_dir), indent=2))
        return 0
    if args.command == "status":
        print(json.dumps(migration_status(database, args.migration_dir), indent=2, default=str))
        return 0
    result = apply_migrations(database, args.migration_dir, args.backup_dir, args.lock_timeout_s)
    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

