"""Config-source conflicts and rolling-cache retention safety.

Two failure modes this pins:

* A key set in `infra/env/midterm.env` can be silently overridden by an
  `environment:` entry in the compose file, because a service's `environment`
  block wins over its `env_file`. Editing the env file then changes nothing and
  the operator has no signal. Every such key must be listed here deliberately.

* An accepted evidence task can outlive the footage it needs. The rolling cache
  evicts by age while the task is still inside its business deadline, so the
  retention window has to cover the whole span from the earliest frame an event
  needs to the last moment a worker may still be reading it -- not merely be
  "larger than the deadline".
"""

from __future__ import annotations

from pathlib import Path
import re

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = REPO_ROOT / "infra" / "env" / "midterm.env"
COMPOSE_FILE = REPO_ROOT / "infra" / "docker-compose.midterm.yml"

# Keys the compose file deliberately overrides or remaps, with why. Anything
# not listed here that conflicts is an accident and fails the test.
#
# These are recorded, NOT endorsed: several look like drift that predates this
# test and needs an owner's decision. The point is that the list cannot grow
# without someone editing it.
KNOWN_COMPOSE_OVERRIDES = {
    "BATCH_SIZE": "compose pins the ingress batch for the replay path",
    "EVIDENCE_EVENT_COVERAGE_WINDOW_SECONDS": "UNRESOLVED: env says 60, compose default 30",
    "FORWARDER_QUEUE_MAX_SIZE": "remapped to REPLAY_RAW_FANOUT_QUEUE_MAX_SIZE",
    "FORWARDER_SEND_HWM": "remapped to REPLAY_RAW_FANOUT_SEND_HWM",
    "FORWARDER_SEND_RETRIES": "remapped to REPLAY_RAW_FANOUT_SEND_RETRIES",
    "FORWARDER_SEND_TIMEOUT_MS": "remapped to REPLAY_RAW_FANOUT_SEND_TIMEOUT_MS",
    "MEDIA_WORKER_FINALIZER_WORKERS": "UNRESOLVED: env says 32, compose default 16",
    "MEDIA_WORKER_SEGMENT_INDEX_ENABLED": "UNRESOLVED: env says true, compose default false",
    "PERSON_OBSERVATION_BATCH_SIZE": "remapped to PERSON_OBSERVATION_WORKER_BATCH_SIZE",
    "POSE_CONFIDENCE_THRESHOLD": "UNRESOLVED: env says 0.60, compose default 0.50",
    "POSE_SELECTOR_CONFIDENCE_THRESHOLD": "UNRESOLVED: env says 0.60, compose default 0.50",
    "STORAGE_MAINTENANCE_EXECUTE_ENABLED": "UNRESOLVED: env says true, compose default false",
}


def _env_file_values() -> dict[str, str]:
    values: dict[str, str] = {}
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def _compose_environment_values() -> dict[str, set[str]]:
    compose = COMPOSE_FILE.read_text(encoding="utf-8")
    found: dict[str, set[str]] = {}
    for match in re.finditer(r'^\s+([A-Z][A-Z0-9_]*):\s*"(.*)"\s*$', compose, re.M):
        found.setdefault(match.group(1), set()).add(match.group(2))
    return found


def _effective(key: str, compose_value: str) -> str:
    """What the compose entry resolves to with no shell value and no .env."""

    match = re.fullmatch(r"\$\{" + re.escape(key) + r":-(.*)\}", compose_value)
    return match.group(1) if match else compose_value


def _conflicts() -> dict[str, tuple[str, str]]:
    env_values = _env_file_values()
    compose_values = _compose_environment_values()
    conflicts: dict[str, tuple[str, str]] = {}
    for key, env_value in env_values.items():
        for compose_value in compose_values.get(key, ()):
            if _effective(key, compose_value) != env_value:
                conflicts[key] = (env_value, compose_value)
                break
    return conflicts


def test_no_undeclared_compose_override_of_the_env_file() -> None:
    """A compose `environment:` entry silently beats `env_file`.

    Without this guard, editing `infra/env/midterm.env` can look like a config
    change and do nothing at all -- which is how a retention or worker-count
    setting ends up different from what everyone believes is deployed.
    """

    conflicts = _conflicts()
    undeclared = sorted(set(conflicts) - set(KNOWN_COMPOSE_OVERRIDES))

    assert not undeclared, (
        "these keys are set in infra/env/midterm.env but silently overridden by "
        "a compose environment: entry — either remove the duplicate, align the "
        "values, or add the key to KNOWN_COMPOSE_OVERRIDES with a reason:\n"
        + "\n".join(
            f"  {key}: env_file={conflicts[key][0]!r} compose={conflicts[key][1]!r}"
            for key in undeclared
        )
    )


def test_declared_overrides_are_all_real() -> None:
    """Keep the exception list honest: a resolved entry must be removed."""

    conflicts = _conflicts()
    stale = sorted(set(KNOWN_COMPOSE_OVERRIDES) - set(conflicts))

    assert not stale, (
        "these keys no longer conflict and should be dropped from "
        f"KNOWN_COMPOSE_OVERRIDES: {stale}"
    )


def _compose_default(key: str) -> str:
    for value in _compose_environment_values().get(key, ()):
        resolved = _effective(key, value)
        if resolved != value or "${" not in value:
            return resolved
    raise AssertionError(f"{key} is not set in any compose environment: block")


def test_rolling_retention_outlives_every_accepted_task() -> None:
    """Retention must cover queueing AND the read that follows it.

    An accepted task can be claimed at the last moment before its business
    deadline and then spend the full processing deadline reading source
    segments. The earliest frame it needs is `pre_seconds` before the event, so
    the footage has to survive:

        pre_seconds + business_deadline + processing_deadline

    Comparing retention against the business deadline alone misses the read
    window entirely, which is how a task that is still "in time" fails because
    its segments were already evicted.
    """

    retention_s = float(_compose_default("ROLLING_CACHE_RETENTION_SECONDS"))
    replay_ttl_s = float(_compose_default("EVIDENCE_REPLAY_TTL_SECONDS"))
    annotation_ttl_s = float(
        _compose_default("EVIDENCE_FRAME_ANNOTATION_TTL_SECONDS")
    )
    pre_seconds = float(_compose_default("DEFAULT_PRE_SECONDS"))
    processing_s = float(
        _compose_default("ROLLING_CACHE_MATERIALIZATION_PROCESSING_DEADLINE_SECONDS")
    )

    # The event-worker takes the earlier of the two TTLs as the deadline.
    business_deadline_s = min(replay_ttl_s, annotation_ttl_s)
    required_s = pre_seconds + business_deadline_s + processing_s

    assert retention_s > required_s, (
        f"rolling retention {retention_s}s does not cover an accepted task: "
        f"pre {pre_seconds}s + deadline {business_deadline_s}s + processing "
        f"{processing_s}s = {required_s}s. A task claimed just before its "
        "deadline would read segments that have already been evicted."
    )


def test_business_deadline_and_event_window_are_unchanged() -> None:
    """Retention is the knob that moved; the product semantics are not.

    Raising the deadline instead would hide scheduling backlog behind a longer
    grace period rather than fixing it.
    """

    assert _compose_default("EVIDENCE_REPLAY_TTL_SECONDS") == "300"
    assert _compose_default("EVIDENCE_FRAME_ANNOTATION_TTL_SECONDS") == "600"
    assert _compose_default("DEFAULT_PRE_SECONDS") == "5"
    assert _compose_default("DEFAULT_POST_SECONDS") == "5"


@pytest.mark.parametrize(
    "key",
    ["ROLLING_CACHE_RETENTION_SECONDS", "EVIDENCE_REPLAY_TTL_SECONDS"],
)
def test_env_file_and_compose_agree_on_evidence_timing(key: str) -> None:
    """The evidence timing keys must not be part of the override problem."""

    env_value = _env_file_values().get(key)
    assert env_value is not None, f"{key} is missing from {ENV_FILE.name}"
    assert env_value == _compose_default(key), (
        f"{key} differs between env_file ({env_value}) and the compose default "
        f"({_compose_default(key)}); the compose value is the one that applies"
    )
