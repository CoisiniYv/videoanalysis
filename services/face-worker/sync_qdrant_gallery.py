#!/usr/bin/env python3
"""Bootstrap, drain, and reconcile the Qdrant face gallery index."""

from __future__ import annotations

import argparse
import json
import logging
import os
import socket
import sys
from dataclasses import dataclass
from typing import Any

import psycopg

from app.config import load_config
from app.gallery_sync_outbox import (
    active_gallery_count,
    active_gallery_rows,
    claim_outbox_rows,
    gallery_row_by_id,
    mark_outbox_completed,
    mark_outbox_failed,
    outbox_status_summary,
)

logger = logging.getLogger("sync-qdrant-gallery")

VECTOR_SIZE = 512


@dataclass(frozen=True)
class SyncConfig:
    database_url: str
    qdrant_url: str
    qdrant_api_key: str
    qdrant_base_collection: str
    qdrant_alias: str
    timeout_seconds: float
    write_wait: bool
    batch_size: int
    max_attempts: int
    claimed_by: str


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode",
        choices=("bootstrap", "drain-outbox", "reconcile", "rebuild", "status"),
        required=True,
    )
    parser.add_argument("--batch-size", type=int, default=int(os.getenv("QDRANT_SYNC_BATCH_SIZE", "500")))
    parser.add_argument("--max-attempts", type=int, default=int(os.getenv("QDRANT_SYNC_MAX_ATTEMPTS", "8")))
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stdout,
    )
    args = parse_args(sys.argv[1:] if argv is None else argv)
    cfg0 = load_config()
    cfg = SyncConfig(
        database_url=cfg0.database_url,
        qdrant_url=cfg0.qdrant_url,
        qdrant_api_key=cfg0.qdrant_api_key,
        qdrant_base_collection=cfg0.qdrant_base_collection,
        qdrant_alias=cfg0.qdrant_collection,
        timeout_seconds=cfg0.qdrant_timeout_seconds,
        write_wait=cfg0.qdrant_write_wait,
        batch_size=max(1, args.batch_size),
        max_attempts=max(1, args.max_attempts),
        claimed_by=f"{socket.gethostname()}:{os.getpid()}",
    )
    client = make_client(cfg)
    with psycopg.connect(cfg.database_url, autocommit=True) as conn:
        if args.mode == "status":
            print(json.dumps(status_summary(conn, client, cfg), ensure_ascii=False, indent=2, default=str))
            return 0
        ensure_collection(client, cfg)
        if args.mode == "rebuild":
            recreate_collection(client, cfg)
            ensure_collection(client, cfg)
            bootstrap(conn, client, cfg)
            reconcile(conn, client, cfg)
        elif args.mode == "bootstrap":
            bootstrap(conn, client, cfg)
        elif args.mode == "drain-outbox":
            drain_outbox(conn, client, cfg)
        elif args.mode == "reconcile":
            reconcile(conn, client, cfg)
        print(json.dumps(status_summary(conn, client, cfg), ensure_ascii=False, indent=2, default=str))
    return 0


def make_client(cfg: SyncConfig) -> Any:
    try:
        from qdrant_client import QdrantClient
    except ModuleNotFoundError as exc:
        raise SystemExit("qdrant-client is not installed in this face-worker image") from exc
    return QdrantClient(
        url=cfg.qdrant_url,
        api_key=cfg.qdrant_api_key or None,
        timeout=cfg.timeout_seconds,
    )


def models() -> Any:
    from qdrant_client import models as qmodels

    return qmodels


def collection_exists(client: Any, name: str) -> bool:
    if hasattr(client, "collection_exists"):
        return bool(client.collection_exists(name))
    try:
        client.get_collection(name)
        return True
    except Exception:
        return False


def ensure_collection(client: Any, cfg: SyncConfig) -> None:
    qmodels = models()
    if not collection_exists(client, cfg.qdrant_base_collection):
        client.create_collection(
            collection_name=cfg.qdrant_base_collection,
            vectors_config=qmodels.VectorParams(
                size=VECTOR_SIZE,
                distance=qmodels.Distance.COSINE,
            ),
        )
        logger.info("qdrant_collection_created collection=%s", cfg.qdrant_base_collection)
    ensure_alias(client, cfg)
    for field_name, schema in (
        ("person_id", qmodels.PayloadSchemaType.INTEGER),
        ("external_person_id", qmodels.PayloadSchemaType.KEYWORD),
        ("embedding_model", qmodels.PayloadSchemaType.KEYWORD),
        ("is_active", qmodels.PayloadSchemaType.BOOL),
        ("is_primary", qmodels.PayloadSchemaType.BOOL),
        ("quality", qmodels.PayloadSchemaType.FLOAT),
    ):
        try:
            client.create_payload_index(
                collection_name=cfg.qdrant_base_collection,
                field_name=field_name,
                field_schema=schema,
                wait=cfg.write_wait,
            )
            logger.info("qdrant_payload_index_ready field=%s", field_name)
        except Exception as exc:
            message = str(exc).lower()
            if "already exists" not in message:
                logger.warning("qdrant_payload_index_skipped field=%s error=%s", field_name, exc)


def ensure_alias(client: Any, cfg: SyncConfig) -> None:
    if cfg.qdrant_alias == cfg.qdrant_base_collection:
        return
    qmodels = models()
    try:
        client.update_collection_aliases(
            change_aliases_operations=[
                qmodels.CreateAliasOperation(
                    create_alias=qmodels.CreateAlias(
                        collection_name=cfg.qdrant_base_collection,
                        alias_name=cfg.qdrant_alias,
                    )
                )
            ]
        )
        logger.info(
            "qdrant_alias_ready alias=%s collection=%s",
            cfg.qdrant_alias,
            cfg.qdrant_base_collection,
        )
    except Exception as exc:
        message = str(exc).lower()
        if "already exists" not in message:
            raise
        logger.info(
            "qdrant_alias_exists alias=%s collection=%s",
            cfg.qdrant_alias,
            cfg.qdrant_base_collection,
        )


def recreate_collection(client: Any, cfg: SyncConfig) -> None:
    if collection_exists(client, cfg.qdrant_base_collection):
        client.delete_collection(collection_name=cfg.qdrant_base_collection)
        logger.info("qdrant_collection_deleted collection=%s", cfg.qdrant_base_collection)


def bootstrap(conn: psycopg.Connection, client: Any, cfg: SyncConfig) -> dict[str, Any]:
    total = 0
    after_id = 0
    while True:
        rows = active_gallery_rows(conn, after_id=after_id, limit=cfg.batch_size)
        if not rows:
            break
        upsert_rows(client, cfg, rows)
        total += len(rows)
        after_id = int(rows[-1]["id"])
    logger.info("qdrant_bootstrap_completed upserted=%d", total)
    return {"upserted": total}


def drain_outbox(conn: psycopg.Connection, client: Any, cfg: SyncConfig) -> dict[str, Any]:
    rows = claim_outbox_rows(conn, claimed_by=cfg.claimed_by, limit=cfg.batch_size)
    processed = 0
    failed = 0
    for row in rows:
        row_id = int(row["id"])
        gallery_id = int(row["gallery_embedding_id"])
        operation = str(row["operation"])
        try:
            if operation == "delete":
                delete_points(client, cfg, [gallery_id])
            else:
                source_row = gallery_row_by_id(conn, gallery_id)
                if source_row is None:
                    delete_points(client, cfg, [gallery_id])
                else:
                    upsert_rows(client, cfg, [source_row])
            mark_outbox_completed(conn, row_id)
            processed += 1
        except Exception as exc:
            failed += 1
            mark_outbox_failed(
                conn,
                row_id,
                error=f"{type(exc).__name__}: {exc}",
                attempts=int(row["attempts"]),
                max_attempts=cfg.max_attempts,
            )
            logger.exception("qdrant_outbox_row_failed id=%s gallery_id=%s", row_id, gallery_id)
    logger.info("qdrant_outbox_drain_completed processed=%d failed=%d", processed, failed)
    return {"claimed": len(rows), "processed": processed, "failed": failed}


def reconcile(conn: psycopg.Connection, client: Any, cfg: SyncConfig) -> dict[str, Any]:
    pg_ids: set[int] = set()
    after_id = 0
    while True:
        rows = active_gallery_rows(conn, after_id=after_id, limit=cfg.batch_size)
        if not rows:
            break
        pg_ids.update(int(row["id"]) for row in rows)
        upsert_rows(client, cfg, rows)
        after_id = int(rows[-1]["id"])

    qdrant_ids = scroll_active_point_ids(client, cfg)
    extra_ids = sorted(qdrant_ids - pg_ids)
    if extra_ids:
        for chunk in _chunks(extra_ids, cfg.batch_size):
            delete_points(client, cfg, chunk)
    result = {
        "postgres_active_count": len(pg_ids),
        "qdrant_active_count_before_delete": len(qdrant_ids),
        "deleted_extra_points": len(extra_ids),
    }
    logger.info("qdrant_reconcile_completed %s", result)
    return result


def status_summary(conn: psycopg.Connection, client: Any, cfg: SyncConfig) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "collection": cfg.qdrant_base_collection,
        "alias": cfg.qdrant_alias,
        "postgres_active_gallery_count": active_gallery_count(conn),
        "outbox": outbox_status_summary(conn),
    }
    try:
        collection = client.get_collection(cfg.qdrant_base_collection)
        summary["qdrant_collection"] = json.loads(collection.model_dump_json())
    except Exception as exc:
        summary["qdrant_collection"] = {"status": "not_available", "error": f"{type(exc).__name__}: {exc}"}
    return summary


def upsert_rows(client: Any, cfg: SyncConfig, rows: list[dict[str, Any]]) -> None:
    qmodels = models()
    points = []
    for row in rows:
        embedding = _coerce_embedding(row["embedding"])
        points.append(
            qmodels.PointStruct(
                id=int(row["id"]),
                vector=embedding,
                payload=gallery_payload(row),
            )
        )
    if not points:
        return
    client.upsert(
        collection_name=cfg.qdrant_base_collection,
        points=points,
        wait=cfg.write_wait,
    )


def delete_points(client: Any, cfg: SyncConfig, ids: list[int]) -> None:
    if not ids:
        return
    qmodels = models()
    client.delete(
        collection_name=cfg.qdrant_base_collection,
        points_selector=qmodels.PointIdsList(points=[int(item) for item in ids]),
        wait=cfg.write_wait,
    )


def scroll_active_point_ids(client: Any, cfg: SyncConfig) -> set[int]:
    qmodels = models()
    point_ids: set[int] = set()
    offset = None
    active_filter = qmodels.Filter(
        must=[
            qmodels.FieldCondition(key="is_active", match=qmodels.MatchValue(value=True)),
            qmodels.FieldCondition(key="embedding_model", match=qmodels.MatchValue(value="adaface")),
        ]
    )
    while True:
        records, offset = client.scroll(
            collection_name=cfg.qdrant_base_collection,
            scroll_filter=active_filter,
            limit=cfg.batch_size,
            offset=offset,
            with_payload=False,
            with_vectors=False,
        )
        point_ids.update(int(record.id) for record in records)
        if offset is None:
            break
    return point_ids


def gallery_payload(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "gallery_embedding_id": int(row["id"]),
        "person_id": int(row["person_id"]),
        "person_name": row.get("person_name") or "",
        "external_person_id": row.get("external_person_id"),
        "source_type": row.get("source_type"),
        "embedding_model": row.get("embedding_model") or "adaface",
        "model_version": row.get("model_version"),
        "is_primary": bool(row.get("is_primary")),
        "is_active": bool(row.get("is_active")),
        "quality": row.get("quality"),
        "created_at": _json_time(row.get("created_at")),
        "updated_at": _json_time(row.get("updated_at")),
    }


def _coerce_embedding(value: Any) -> list[float]:
    if hasattr(value, "tolist"):
        return [float(item) for item in value.tolist()]
    return [float(item) for item in value]


def _json_time(value: Any) -> str | None:
    if value is None:
        return None
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def _chunks(values: list[int], size: int) -> list[list[int]]:
    return [values[index:index + size] for index in range(0, len(values), size)]


if __name__ == "__main__":
    raise SystemExit(main())
