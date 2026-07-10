from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


DEFAULT_SHARD_ID = "default"


class ReplayShardConfigError(ValueError):
    pass


@dataclass(frozen=True)
class ReplayShard:
    shard_id: str
    replay_api_url: str
    in_stream_endpoint: str
    replay_job_sink_url: str
    source_ids: frozenset[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "shard_id": self.shard_id,
            "replay_api_url": self.replay_api_url,
            "in_stream_endpoint": self.in_stream_endpoint,
            "replay_job_sink_url": self.replay_job_sink_url,
            "source_ids": sorted(self.source_ids),
        }


@dataclass(frozen=True)
class ReplayShardMap:
    shards: tuple[ReplayShard, ...]
    default_shard_id: str
    explicit: bool
    mapping_version: str = "implicit-default"

    def __post_init__(self) -> None:
        if not self.shards:
            raise ReplayShardConfigError("at least one replay shard is required")
        shard_ids = [shard.shard_id for shard in self.shards]
        if len(set(shard_ids)) != len(shard_ids):
            raise ReplayShardConfigError("replay shard ids must be unique")
        if self.default_shard_id not in set(shard_ids):
            raise ReplayShardConfigError(
                f"default replay shard {self.default_shard_id!r} is not configured"
            )

    @property
    def enabled(self) -> bool:
        return self.explicit and len(self.shards) > 1

    def shard_for_source(self, source_id: str) -> ReplayShard:
        text = str(source_id or "").strip()
        if not text:
            raise ReplayShardConfigError("source_id is required for replay shard routing")
        for shard in self.shards:
            if text in shard.source_ids:
                return shard
        if self.explicit:
            raise ReplayShardConfigError(
                f"source_id {text!r} is not assigned to any replay shard"
            )
        return self.shard_by_id(self.default_shard_id)

    def shard_by_id(self, shard_id: str) -> ReplayShard:
        text = str(shard_id or "").strip()
        for shard in self.shards:
            if shard.shard_id == text:
                return shard
        raise ReplayShardConfigError(f"unknown replay shard {text!r}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "explicit": self.explicit,
            "default_shard_id": self.default_shard_id,
            "mapping_version": self.mapping_version,
            "shards": [shard.to_dict() for shard in self.shards],
        }


def load_replay_shard_map(
    *,
    default_replay_api_url: str,
    default_in_stream_endpoint: str,
    default_replay_job_sink_url: str,
    env: Mapping[str, str] | None = None,
    env_var: str = "REPLAY_SHARDS_JSON",
    path_env_var: str = "REPLAY_SHARDS_CONFIG_PATH",
) -> ReplayShardMap:
    values = os.environ if env is None else env
    raw = str(values.get(env_var) or "").strip()
    if not raw:
        config_path = str(values.get(path_env_var) or "").strip()
        if config_path:
            raw = Path(config_path).read_text(encoding="utf-8")
    if not raw:
        return ReplayShardMap(
            shards=(
                ReplayShard(
                    shard_id=DEFAULT_SHARD_ID,
                    replay_api_url=default_replay_api_url,
                    in_stream_endpoint=default_in_stream_endpoint,
                    replay_job_sink_url=default_replay_job_sink_url,
                    source_ids=frozenset(),
                ),
            ),
            default_shard_id=DEFAULT_SHARD_ID,
            explicit=False,
            mapping_version="implicit-default",
        )
    try:
        doc = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ReplayShardConfigError(f"{env_var} is not valid JSON: {exc}") from exc
    return parse_replay_shard_map(
        doc,
        default_replay_api_url=default_replay_api_url,
        default_in_stream_endpoint=default_in_stream_endpoint,
        default_replay_job_sink_url=default_replay_job_sink_url,
    )


def parse_replay_shard_map(
    doc: object,
    *,
    default_replay_api_url: str,
    default_in_stream_endpoint: str,
    default_replay_job_sink_url: str,
) -> ReplayShardMap:
    if not isinstance(doc, Mapping):
        raise ReplayShardConfigError("replay shard config must be a JSON object")
    raw_shards = doc.get("shards")
    if not isinstance(raw_shards, list) or not raw_shards:
        raise ReplayShardConfigError("replay shard config requires a non-empty shards list")
    default_shard_id = str(doc.get("default_shard_id") or "").strip()
    mapping_version = str(
        doc.get("mapping_version")
        or doc.get("version")
        or "unversioned"
    ).strip()
    if not mapping_version:
        mapping_version = "unversioned"
    shards: list[ReplayShard] = []
    assigned_sources: dict[str, str] = {}
    for raw_shard in raw_shards:
        if not isinstance(raw_shard, Mapping):
            raise ReplayShardConfigError("each replay shard must be a JSON object")
        shard_id = str(raw_shard.get("shard_id") or "").strip()
        if not shard_id:
            raise ReplayShardConfigError("replay shard requires shard_id")
        source_ids = _source_ids(raw_shard.get("source_ids"))
        for source_id in source_ids:
            previous = assigned_sources.get(source_id)
            if previous and previous != shard_id:
                raise ReplayShardConfigError(
                    f"source_id {source_id!r} assigned to both {previous!r} and {shard_id!r}"
                )
            assigned_sources[source_id] = shard_id
        shards.append(
            ReplayShard(
                shard_id=shard_id,
                replay_api_url=_required_url(
                    raw_shard,
                    "replay_api_url",
                    default_replay_api_url,
                ),
                in_stream_endpoint=_required_url(
                    raw_shard,
                    "in_stream_endpoint",
                    default_in_stream_endpoint,
                ),
                replay_job_sink_url=_required_url(
                    raw_shard,
                    "replay_job_sink_url",
                    default_replay_job_sink_url,
                ),
                source_ids=frozenset(source_ids),
            )
        )
    if not default_shard_id:
        default_shard_id = shards[0].shard_id
    return ReplayShardMap(
        shards=tuple(shards),
        default_shard_id=default_shard_id,
        explicit=True,
        mapping_version=mapping_version,
    )


def _source_ids(raw: object) -> set[str]:
    if raw is None:
        return set()
    if not isinstance(raw, list):
        raise ReplayShardConfigError("source_ids must be a list")
    out: set[str] = set()
    for value in raw:
        text = str(value or "").strip()
        if not text:
            raise ReplayShardConfigError("source_ids cannot contain empty values")
        out.add(text)
    return out


def _required_url(raw: Mapping[str, object], key: str, fallback: str) -> str:
    value = str(raw.get(key) or fallback or "").strip()
    if not value:
        raise ReplayShardConfigError(f"replay shard requires {key}")
    return value
