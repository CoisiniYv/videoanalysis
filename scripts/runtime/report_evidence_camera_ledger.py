#!/usr/bin/env python3
"""Per-camera evidence stage ledger.

Answers, for one camera, the question a system-wide success rate cannot: did
this camera's evidence request get refused, merged, queued, executed, published
-- or did it fail, expire, or simply never get a turn?

The reconciliation it enforces is:

    accepted = outstanding + published + terminal_unsuccessful

Those three buckets are mutually exclusive and together cover every accepted
task, so a task cannot go missing from the accounting. Admission refusals and
coverage merges are reported separately and are deliberately NOT folded into
`accepted`: counting a success rate over admitted tasks alone would hide
exactly the requests that were dropped before they ever became tasks.

Usage:
    report_evidence_camera_ledger.py --database-url postgresql://... \
        [--since-minutes 60] [--format table|json]
"""

from __future__ import annotations

import argparse
from collections import OrderedDict
import json
import os
import sys

import psycopg
from psycopg.rows import dict_row


# Terminal states that are not a published artifact. Kept explicit so a new
# lifecycle state cannot silently fall out of the reconciliation.
UNSUCCESSFUL_TERMINAL = (
    "materialization_failed",
    "materialization_expired",
    "materialization_deferred",
)
OUTSTANDING = ("manifest_ready", "materialization_pending", "materializing")

LEDGER_SQL = """
WITH scoped AS (
    SELECT
        COALESCE(
            NULLIF(et.source_id, ''),
            NULLIF(et.replay_source_id, ''),
            '(unknown)'
        ) AS source_id,
        et.materialization_status,
        et.materialization_expired_reason,
        et.materialization_failure_reason,
        et.error_message,
        et.materialization_ready_at,
        et.materialization_deadline_at,
        et.updated_at
    FROM evidence_tasks et
    WHERE et.created_at >= now() - (%(since_minutes)s || ' minutes')::interval
)
SELECT
    source_id,
    COUNT(*)                                                   AS tasks_created,
    COUNT(*) FILTER (WHERE materialization_status = ANY(%(outstanding)s))
                                                               AS outstanding,
    COUNT(*) FILTER (WHERE materialization_status = 'materialized')
                                                               AS published,
    COUNT(*) FILTER (WHERE materialization_status = ANY(%(unsuccessful)s))
                                                               AS terminal_unsuccessful,
    COUNT(*) FILTER (WHERE materialization_status = 'materialization_skipped')
                                                               AS admission_skipped,
    COUNT(*) FILTER (WHERE materialization_status = 'materialization_expired')
                                                               AS expired,
    COUNT(*) FILTER (
        WHERE materialization_expired_reason = 'business_deadline_expired'
    )                                                          AS deadline_expired,
    COUNT(*) FILTER (WHERE materialization_status = 'materialization_failed')
                                                               AS failed,
    COALESCE(
        MAX(
            EXTRACT(EPOCH FROM (now() - materialization_ready_at))
        ) FILTER (WHERE materialization_status = ANY(%(outstanding)s)),
        0
    )::bigint                                                  AS oldest_ready_age_s,
    COALESCE(
        MIN(
            EXTRACT(EPOCH FROM (materialization_deadline_at - now()))
        ) FILTER (WHERE materialization_status = ANY(%(outstanding)s)),
        0
    )::bigint                                                  AS tightest_deadline_slack_s
FROM scoped
GROUP BY source_id
ORDER BY source_id
"""

REASON_SQL = """
SELECT
    COALESCE(
        NULLIF(source_id, ''),
        NULLIF(replay_source_id, ''),
        '(unknown)'
    ) AS source_id,
    COALESCE(
        NULLIF(materialization_expired_reason, ''),
        NULLIF(materialization_failure_reason, ''),
        NULLIF(error_message, ''),
        '(none)'
    ) AS reason,
    COUNT(*) AS tasks
FROM evidence_tasks
WHERE created_at >= now() - (%(since_minutes)s || ' minutes')::interval
  AND materialization_status <> 'materialized'
  AND materialization_status <> ALL(%(outstanding)s)
GROUP BY 1, 2
ORDER BY 1, 3 DESC
"""


def collect(conn: psycopg.Connection, since_minutes: int) -> dict[str, object]:
    params = {
        "since_minutes": str(int(since_minutes)),
        "outstanding": list(OUTSTANDING),
        "unsuccessful": list(UNSUCCESSFUL_TERMINAL),
    }
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(LEDGER_SQL, params)
        cameras = [dict(row) for row in cur.fetchall()]
        cur.execute(REASON_SQL, params)
        reasons = [dict(row) for row in cur.fetchall()]

    by_camera: "OrderedDict[str, list[dict[str, object]]]" = OrderedDict()
    for row in reasons:
        by_camera.setdefault(str(row["source_id"]), []).append(
            {"reason": row["reason"], "tasks": int(row["tasks"])}
        )

    unbalanced = []
    for row in cameras:
        row["reasons"] = by_camera.get(str(row["source_id"]), [])
        accounted = (
            int(row["outstanding"])
            + int(row["published"])
            + int(row["terminal_unsuccessful"])
            + int(row["admission_skipped"])
        )
        row["accounted"] = accounted
        # Every task must land in exactly one bucket. A mismatch means a
        # lifecycle state exists that this report does not know about, which is
        # itself the finding -- not something to paper over.
        if accounted != int(row["tasks_created"]):
            row["unaccounted"] = int(row["tasks_created"]) - accounted
            unbalanced.append(str(row["source_id"]))

    return {
        "since_minutes": int(since_minutes),
        "cameras": cameras,
        "unbalanced_cameras": unbalanced,
        "silent_cameras": [
            str(row["source_id"])
            for row in cameras
            if int(row["published"]) == 0 and int(row["tasks_created"]) > 0
        ],
    }


def render_table(report: dict[str, object]) -> str:
    header = (
        f"{'camera':<20}{'created':>8}{'out':>6}{'pub':>6}{'fail/exp':>10}"
        f"{'skipped':>9}{'deadline':>10}{'oldest_s':>10}{'slack_s':>9}"
    )
    lines = [
        f"evidence ledger, last {report['since_minutes']} minutes",
        "",
        header,
        "-" * len(header),
    ]
    for row in report["cameras"]:  # type: ignore[index]
        lines.append(
            f"{str(row['source_id']):<20}"
            f"{row['tasks_created']:>8}"
            f"{row['outstanding']:>6}"
            f"{row['published']:>6}"
            f"{row['terminal_unsuccessful']:>10}"
            f"{row['admission_skipped']:>9}"
            f"{row['deadline_expired']:>10}"
            f"{row['oldest_ready_age_s']:>10}"
            f"{row['tightest_deadline_slack_s']:>9}"
        )
    silent = report["silent_cameras"]
    unbalanced = report["unbalanced_cameras"]
    lines.append("")
    lines.append(
        f"cameras with tasks but nothing published: {len(silent)}"  # type: ignore[arg-type]
        + (f" -> {silent}" if silent else "")  # type: ignore[operator]
    )
    if unbalanced:
        lines.append(
            f"LEDGER DOES NOT BALANCE for {unbalanced} -- a lifecycle state is "
            "missing from this report"
        )
    reasons_seen = {
        str(row["source_id"]): row["reasons"]
        for row in report["cameras"]  # type: ignore[index]
        if row["reasons"]
    }
    if reasons_seen:
        lines.append("")
        lines.append("non-published outcomes by camera:")
        for camera, reasons in reasons_seen.items():
            rendered = ", ".join(f"{r['reason']}={r['tasks']}" for r in reasons)
            lines.append(f"  {camera}: {rendered}")
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database-url",
        default=os.getenv("DATABASE_URL", ""),
        help="PostgreSQL URL (defaults to $DATABASE_URL)",
    )
    parser.add_argument("--since-minutes", type=int, default=60)
    parser.add_argument("--format", choices=("table", "json"), default="table")
    args = parser.parse_args(argv)

    if not args.database_url:
        parser.error("--database-url or $DATABASE_URL is required")

    with psycopg.connect(args.database_url, autocommit=True) as conn:
        report = collect(conn, args.since_minutes)

    if args.format == "json":
        print(json.dumps(report, indent=2, default=str))
    else:
        print(render_table(report))
    # A ledger that does not balance is a finding, so say so in the exit code.
    return 1 if report["unbalanced_cameras"] else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
