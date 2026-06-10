-- Migration 012: Operator camera schema compatibility.
--
-- Some long-lived runtime databases still have the placeholder camera tables
-- from 001_init.sql (UUID ids, stream_url/name/polygon shape). The operator
-- camera API expects the later midterm columns. This migration is intentionally
-- non-destructive: it adds and backfills the missing columns without dropping
-- camera tables or dependent foreign keys.

ALTER TABLE cameras
    ADD COLUMN IF NOT EXISTS source_id TEXT,
    ADD COLUMN IF NOT EXISTS rtsp_url TEXT,
    ADD COLUMN IF NOT EXISTS site_id TEXT,
    ADD COLUMN IF NOT EXISTS location TEXT,
    ADD COLUMN IF NOT EXISTS gpu_id INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS input_type TEXT NOT NULL DEFAULT 'rtsp',
    ADD COLUMN IF NOT EXISTS rtsp_transport TEXT NOT NULL DEFAULT 'tcp',
    ADD COLUMN IF NOT EXISTS fps_policy JSONB NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS alert_policy JSONB NOT NULL DEFAULT '{}'::jsonb;

UPDATE cameras
SET source_id = id::text
WHERE source_id IS NULL OR source_id = '';

DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'cameras'
          AND column_name = 'stream_url'
    ) THEN
        EXECUTE
            'UPDATE cameras
             SET rtsp_url = stream_url
             WHERE (rtsp_url IS NULL OR rtsp_url = '''')
               AND stream_url IS NOT NULL';
    END IF;
END $$;

UPDATE cameras
SET rtsp_url = ''
WHERE rtsp_url IS NULL;

ALTER TABLE cameras
    ALTER COLUMN source_id SET NOT NULL,
    ALTER COLUMN rtsp_url SET NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS cameras_source_id_unique_idx
    ON cameras(source_id);

CREATE INDEX IF NOT EXISTS cameras_enabled_idx
    ON cameras(enabled);

ALTER TABLE camera_zones
    ADD COLUMN IF NOT EXISTS zone_id TEXT,
    ADD COLUMN IF NOT EXISTS zone_name TEXT,
    ADD COLUMN IF NOT EXISTS zone_type TEXT,
    ADD COLUMN IF NOT EXISTS coordinate_space TEXT NOT NULL DEFAULT 'pixel',
    ADD COLUMN IF NOT EXISTS points JSONB,
    ADD COLUMN IF NOT EXISTS payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS enabled BOOLEAN NOT NULL DEFAULT TRUE;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'camera_zones'
          AND column_name = 'name'
    ) THEN
        ALTER TABLE camera_zones
            ALTER COLUMN name DROP NOT NULL,
            ALTER COLUMN name SET DEFAULT '';
        EXECUTE
            'UPDATE camera_zones
             SET zone_name = name
             WHERE (zone_name IS NULL OR zone_name = '''')
               AND name IS NOT NULL';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'camera_zones'
          AND column_name = 'polygon'
    ) THEN
        ALTER TABLE camera_zones
            ALTER COLUMN polygon SET DEFAULT '[]'::jsonb;
        EXECUTE
            'UPDATE camera_zones
             SET points = polygon
             WHERE points IS NULL
               AND polygon IS NOT NULL';
    END IF;
END $$;

UPDATE camera_zones
SET zone_id = COALESCE(NULLIF(zone_id, ''), id::text)
WHERE zone_id IS NULL OR zone_id = '';

UPDATE camera_zones
SET zone_name = COALESCE(NULLIF(zone_name, ''), zone_id)
WHERE zone_name IS NULL OR zone_name = '';

UPDATE camera_zones
SET zone_type = COALESCE(NULLIF(zone_type, ''), 'polygon')
WHERE zone_type IS NULL OR zone_type = '';

UPDATE camera_zones
SET points = '[]'::jsonb
WHERE points IS NULL;

ALTER TABLE camera_zones
    ALTER COLUMN zone_id SET NOT NULL,
    ALTER COLUMN zone_name SET NOT NULL,
    ALTER COLUMN zone_type SET NOT NULL,
    ALTER COLUMN points SET NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS camera_zones_camera_zone_id_unique_idx
    ON camera_zones(camera_id, zone_id);

CREATE INDEX IF NOT EXISTS camera_zones_camera_zone_id_idx
    ON camera_zones(camera_id, zone_id);

ALTER TABLE camera_rules
    ADD COLUMN IF NOT EXISTS rule_id TEXT,
    ADD COLUMN IF NOT EXISTS algorithm_id TEXT,
    ADD COLUMN IF NOT EXISTS line_id TEXT,
    ADD COLUMN IF NOT EXISTS evidence_policy JSONB NOT NULL DEFAULT '{}'::jsonb;

UPDATE camera_rules
SET rule_id = COALESCE(NULLIF(rule_id, ''), 'rule_' || id::text),
    algorithm_id = COALESCE(NULLIF(algorithm_id, ''), NULLIF(rule_type, ''), 'unknown')
WHERE rule_id IS NULL
   OR rule_id = ''
   OR algorithm_id IS NULL
   OR algorithm_id = '';

ALTER TABLE camera_rules
    ALTER COLUMN rule_id SET NOT NULL,
    ALTER COLUMN algorithm_id SET NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS camera_rules_camera_rule_id_unique_idx
    ON camera_rules(camera_id, rule_id);

CREATE INDEX IF NOT EXISTS camera_rules_camera_rule_id_idx
    ON camera_rules(camera_id, rule_id);

CREATE INDEX IF NOT EXISTS camera_rules_camera_algorithm_id_idx
    ON camera_rules(camera_id, algorithm_id);
