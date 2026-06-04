"""Face observation worker — consume face observations from Redis Stream → PostgreSQL."""

from __future__ import annotations

import json
import logging
import math
import signal
import sys
import time
from typing import Dict

import psycopg
from psycopg.rows import dict_row
from redis import Redis

from app.config import Config
from app.face_match_event_service import (
    build_watchlist_hit_event,
    publish_security_event,
)
from app.redis_consumer import RedisStreamConsumer
from app.repository import FaceObservationRepository
from app.vector_store import FaceVectorStore

logger = logging.getLogger(__name__)

shutdown_requested = False

# Validation thresholds for embedding quality
_EMBEDDING_DIM = 512
_MIN_EMBEDDING_NORM = 0.90
_MAX_EMBEDDING_NORM = 1.10


class WatchlistMatchEmitter:
    """Emit watchlist_hit events for newly persisted face observations."""

    def __init__(
        self,
        cfg: Config,
        conn: psycopg.Connection,
        redis_client: Redis,
    ) -> None:
        self._cfg = cfg
        self._conn = conn
        self._redis = redis_client
        self._store = FaceVectorStore(conn)
        self._target_person_ids: list[int] | None = None
        self._last_target_refresh = 0.0

    def emit_for_observation(self, obs: dict) -> int:
        """Search configured watchlist targets and emit events above threshold."""
        target_person_ids = self._current_target_person_ids()
        if target_person_ids == []:
            logger.warning(
                "watchlist targets configured but no active gallery persons resolved"
            )
            return 0

        gallery_results = self._store.search_gallery(
            [float(x) for x in obs["embedding"]],
            top_k=max(self._cfg.watchlist_top_k, 1),
            min_similarity=self._cfg.watchlist_threshold,
            person_ids=target_person_ids,
        )
        emitted = 0
        seen_person_ids: set[int] = set()
        for gallery_match in gallery_results:
            person_id = int(gallery_match["person_id"])
            if person_id in seen_person_ids:
                continue
            seen_person_ids.add(person_id)
            event = build_watchlist_hit_event(
                observation=obs,
                gallery_match=gallery_match,
                threshold=self._cfg.watchlist_threshold,
                event_type="watchlist_hit",
            )
            publish_security_event(
                self._redis,
                event,
                stream=self._cfg.watchlist_event_stream,
            )
            emitted += 1
            logger.info(
                "watchlist_hit_emitted source_observation_id=%s person_id=%s "
                "person_name=%s similarity=%.4f threshold=%.4f",
                obs.get("source_observation_id"),
                person_id,
                gallery_match.get("person_name", ""),
                float(gallery_match["similarity"]),
                self._cfg.watchlist_threshold,
            )
        return emitted

    def _target_filters_configured(self) -> bool:
        return bool(
            self._cfg.watchlist_target_external_person_ids
            or self._cfg.watchlist_target_names
        )

    def _current_target_person_ids(self) -> list[int] | None:
        if not self._target_filters_configured():
            return None

        now = time.monotonic()
        refresh_seconds = max(self._cfg.watchlist_target_refresh_seconds, 1)
        if (
            self._target_person_ids is not None
            and now - self._last_target_refresh < refresh_seconds
        ):
            return self._target_person_ids

        external_ids = [
            value.lower()
            for value in self._cfg.watchlist_target_external_person_ids
        ]
        names = [value.lower() for value in self._cfg.watchlist_target_names]
        clauses: list[str] = []
        params: dict[str, list[str]] = {}
        if external_ids:
            clauses.append(
                "lower(COALESCE(external_person_id, '')) = "
                "ANY(%(external_ids)s::text[])"
            )
            params["external_ids"] = external_ids
        if names:
            clauses.append("lower(name) = ANY(%(names)s::text[])")
            params["names"] = names

        with self._conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                f"""
                SELECT id, name, external_person_id
                FROM persons
                WHERE is_active = true
                  AND ({" OR ".join(clauses)})
                ORDER BY id
                """,
                params,
            )
            rows = list(cur.fetchall())

        self._target_person_ids = [int(row["id"]) for row in rows]
        self._last_target_refresh = now
        logger.info(
            "watchlist targets refreshed count=%d external_ids=%s names=%s",
            len(self._target_person_ids),
            self._cfg.watchlist_target_external_person_ids,
            self._cfg.watchlist_target_names,
        )
        return self._target_person_ids


def request_shutdown(signum: int, _frame: object) -> None:
    global shutdown_requested
    logger.info("shutdown requested by signal=%s", signum)
    shutdown_requested = True


def _parse_observation(fields: Dict[bytes, bytes]) -> dict | None:
    """Parse a face observation from Redis stream fields.

    The ``data`` field contains the full JSON payload. Returns the parsed
    dict on success, or None if the entry is unparseable / missing required
    identity fields.
    """
    data_raw = fields.get(b"data")
    if not data_raw:
        logger.warning("stream entry missing data field, skipping")
        return None

    try:
        obs = json.loads(data_raw)
    except (json.JSONDecodeError, TypeError) as exc:
        logger.warning("failed to parse observation JSON: %s", exc)
        return None

    required = ["source_observation_id", "camera_id", "track_id"]
    for field in required:
        if not obs.get(field):
            logger.warning(
                "observation missing required field=%s, skipping", field
            )
            return None

    return obs


def _coerce_finite_float(value, idx: int) -> float | None:
    """Try to convert *value* to a finite float. Returns None if invalid.

    Only int and float types are accepted. Strings (including "nan", "inf",
    "-inf") and non-numeric values are rejected. The result is checked with
    ``math.isfinite`` so NaN, inf, and -inf are all rejected.
    """
    if not isinstance(value, (int, float)):
        return None
    if isinstance(value, bool):
        return None
    f = float(value)
    if not math.isfinite(f):
        return None
    return f


def _validate_embedding(obs: dict, msg_id: str) -> str | None:
    """Validate embedding fields in a parsed observation.

    Returns an error message string if invalid, or None if valid.
    Invalid embedding means the entry is well-formed but has a missing,
    wrong-dimension, NaN/inf element, or out-of-range embedding — this is a
    producer bug, NOT consumer noise, so the message should be left unacked.
    """
    embedding = obs.get("embedding")
    if embedding is None:
        return "embedding field missing"
    if not isinstance(embedding, list):
        return f"embedding is not a list (type={type(embedding).__name__})"
    if len(embedding) != _EMBEDDING_DIM:
        return f"embedding length={len(embedding)}, expected {_EMBEDDING_DIM}"

    # Validate every element: must be int/float, finite (no NaN/inf)
    for i, val in enumerate(embedding):
        if _coerce_finite_float(val, i) is None:
            return (
                f"embedding[{i}] invalid: type={type(val).__name__} "
                f"value={repr(val)}"
            )

    embedding_dim = obs.get("embedding_dim")
    if embedding_dim is None:
        return "embedding_dim field missing"
    if int(embedding_dim) != _EMBEDDING_DIM:
        return f"embedding_dim={embedding_dim}, expected {_EMBEDDING_DIM}"

    embedding_norm = obs.get("embedding_norm")
    if embedding_norm is None:
        return "embedding_norm field missing"
    try:
        norm = float(embedding_norm)
    except (ValueError, TypeError):
        return f"embedding_norm cannot be converted to float: {repr(embedding_norm)}"
    if not math.isfinite(norm):
        return f"embedding_norm is not finite: {norm}"
    if not (_MIN_EMBEDDING_NORM <= norm <= _MAX_EMBEDDING_NORM):
        return f"embedding_norm={norm} out of range [{_MIN_EMBEDDING_NORM}, {_MAX_EMBEDDING_NORM}]"

    return None


def _handle_observation(
    obs: dict,
    msg_id: str,
    repo: FaceObservationRepository,
    consumer: RedisStreamConsumer,
) -> tuple[str, str | None]:
    """Process a single observation: insert into DB, then ACK on success.

    Returns ``(outcome, obs_id)`` where outcome is one of:
    - ``"inserted"`` — new row inserted (ACKed)
    - ``"duplicate"`` — source_observation_id conflict, no-op (ACKed)
    - ``"failed"`` — DB insert exception (NOT ACKed)
    """
    try:
        obs_id = repo.insert_observation(obs)
    except Exception:
        logger.exception(
            "db insert failed for source_observation_id=%s msg_id=%s",
            obs.get("source_observation_id"),
            msg_id,
        )
        # Do NOT ack — leave pending so the failure surfaces in monitoring
        return "failed", None

    if obs_id is not None:
        # New row inserted
        if not consumer.ack(msg_id):
            logger.error("ack failed for msg_id=%s", msg_id)
        return "inserted", obs.get("source_observation_id", "")
    else:
        # ON CONFLICT DO NOTHING — duplicate, safe to ACK
        if not consumer.ack(msg_id):
            logger.error("ack failed for msg_id=%s", msg_id)
        return "duplicate", obs.get("source_observation_id", "")


def _process_batch(
    messages: list[tuple[str, dict[bytes, bytes]]],
    repo: FaceObservationRepository,
    consumer: RedisStreamConsumer,
    watchlist_emitter: WatchlistMatchEmitter | None = None,
) -> tuple[int, int, int, int, int]:
    inserted = 0
    duplicates = 0
    skipped = 0
    failed = 0
    watchlist_emitted = 0
    for msg_id, fields in messages:
        obs = _parse_observation(fields)
        if obs is None:
            # Unparseable / missing identity — safe to ACK as garbage
            consumer.ack(msg_id)
            continue

        embed_err = _validate_embedding(obs, msg_id)
        if embed_err:
            logger.error(
                "msg_id=%s embedding invalid, left pending: %s", msg_id, embed_err
            )
            skipped += 1
            # Do NOT ack — leave pending so it surfaces in monitoring.
            # This is a producer bug, not consumer noise.
            continue

        outcome, _ = _handle_observation(obs, msg_id, repo, consumer)
        if outcome == "inserted":
            inserted += 1
            if watchlist_emitter is not None:
                try:
                    watchlist_emitted += watchlist_emitter.emit_for_observation(obs)
                except Exception:
                    logger.exception(
                        "watchlist emit failed source_observation_id=%s",
                        obs.get("source_observation_id"),
                    )
        elif outcome == "duplicate":
            duplicates += 1
        elif outcome == "failed":
            failed += 1
            # DB insert exception — NOT ACKed, left pending
    return inserted, duplicates, skipped, failed, watchlist_emitted


def connect_redis(cfg: Config) -> Redis:
    client = Redis.from_url(cfg.redis_url, decode_responses=False)
    client.ping()
    logger.info("connected to redis url=%s", cfg.redis_url)
    return client


def connect_postgres(cfg: Config) -> psycopg.Connection:
    conn = psycopg.connect(cfg.database_url, autocommit=True)
    with conn.cursor() as cur:
        cur.execute("SELECT 1")
        cur.fetchone()
    logger.info("connected to postgres url=%s", cfg.database_url)
    return conn


def run_worker(
    cfg: Config,
    redis_client: Redis,
    pg_conn: psycopg.Connection,
) -> None:
    consumer = RedisStreamConsumer(
        redis_client,
        cfg.face_observation_stream,
        cfg.consumer_group,
        cfg.consumer_name,
        start_id=cfg.consumer_start_id,
    )
    consumer.ensure_group()
    repo = FaceObservationRepository(pg_conn)
    watchlist_emitter = (
        WatchlistMatchEmitter(cfg, pg_conn, redis_client)
        if cfg.watchlist_match_enabled
        else None
    )

    logger.info(
        "face-worker started stream=%s group=%s consumer=%s start_id=%s "
        "watchlist_match_enabled=%s watchlist_event_stream=%s "
        "watchlist_threshold=%.4f watchlist_targets=%s/%s",
        cfg.face_observation_stream,
        cfg.consumer_group,
        cfg.consumer_name,
        cfg.consumer_start_id,
        cfg.watchlist_match_enabled,
        cfg.watchlist_event_stream,
        cfg.watchlist_threshold,
        cfg.watchlist_target_external_person_ids,
        cfg.watchlist_target_names,
    )

    total_inserted = 0
    total_duplicates = 0
    total_skipped = 0
    total_failed = 0
    total_watchlist_emitted = 0
    last_report = time.monotonic()

    while not shutdown_requested:
        try:
            # 1. Process pending messages (recovery)
            pending = consumer.read_pending(count=cfg.batch_size)
            if pending:
                ins, dup, skip, fail, wl = _process_batch(
                    pending,
                    repo,
                    consumer,
                    watchlist_emitter,
                )
                total_inserted += ins
                total_duplicates += dup
                total_skipped += skip
                total_failed += fail
                total_watchlist_emitted += wl
                if ins or dup or skip or fail or wl:
                    logger.info(
                        "pending batch: inserted=%d duplicates=%d skipped=%d "
                        "failed=%d watchlist_emitted=%d",
                        ins, dup, skip, fail, wl,
                    )

            # 2. Read new messages
            new_msgs = consumer.read_new(
                count=cfg.batch_size, block_ms=cfg.poll_timeout_ms
            )
            if new_msgs:
                ins, dup, skip, fail, wl = _process_batch(
                    new_msgs,
                    repo,
                    consumer,
                    watchlist_emitter,
                )
                total_inserted += ins
                total_duplicates += dup
                total_skipped += skip
                total_failed += fail
                total_watchlist_emitted += wl
                logger.info(
                    "new batch: inserted=%d duplicates=%d skipped=%d failed=%d "
                    "watchlist_emitted=%d",
                    ins,
                    dup,
                    skip,
                    fail,
                    wl,
                )

            # 3. Periodic summary
            now = time.monotonic()
            if now - last_report >= 60:
                logger.info(
                    "worker summary: total_inserted=%d total_duplicates=%d "
                    "total_skipped=%d total_failed=%d total_watchlist_emitted=%d",
                    total_inserted,
                    total_duplicates,
                    total_skipped,
                    total_failed,
                    total_watchlist_emitted,
                )
                last_report = now

        except Exception:
            logger.exception("worker loop error, sleeping 1s")
            time.sleep(1)

    logger.info(
        "face-worker stopped: total_inserted=%d total_duplicates=%d "
        "total_skipped=%d total_failed=%d total_watchlist_emitted=%d",
        total_inserted,
        total_duplicates,
        total_skipped,
        total_failed,
        total_watchlist_emitted,
    )
