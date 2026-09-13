-- Migration 033: durable per-source scheduling rotation for evidence lanes.
--
-- Ranking candidates per source makes a single scheduler window fair, but the
-- ranking is recomputed every poll and remembers nothing about which cameras
-- were served last. With more cameras than window slots, a camera carrying a
-- deep backlog supplies the oldest rank-1 row every poll and quieter cameras
-- are never selected at all -- they simply age out at their business deadline.
--
-- This table is the missing memory. The scheduler picks which sources to serve
-- from it, and advances a source's cursor only when a task of that source is
-- actually claimed, so a rejected claim or a coverage deferral never counts as
-- service. It lives in the database rather than in scheduler memory so that
-- several scheduler processes converge on one rotation instead of each
-- believing itself fair.

CREATE SEQUENCE IF NOT EXISTS evidence_source_rotation_seq;

CREATE TABLE IF NOT EXISTS evidence_source_rotation (
    lane            TEXT        NOT NULL,
    source_id       TEXT        NOT NULL,
    served_seq      BIGINT      NOT NULL,
    served_count    BIGINT      NOT NULL DEFAULT 0,
    last_served_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (lane, source_id)
);

-- The selection query orders by (lane, served_seq) and takes a handful of
-- rows, so this keeps it an index scan rather than a sort of every source.
CREATE INDEX IF NOT EXISTS evidence_source_rotation_lane_seq_idx
    ON evidence_source_rotation (lane, served_seq ASC);

COMMENT ON TABLE evidence_source_rotation IS
    'Per-lane round-robin cursor over evidence source ids. A source advances '
    'only on a successful task claim, giving every ready source a bounded '
    'wait even when there are more sources than scheduler slots.';

COMMENT ON COLUMN evidence_source_rotation.served_seq IS
    'Monotonic service order from evidence_source_rotation_seq. Lower means '
    'served longer ago; a source absent from this table has never been served '
    'and is selected first.';
