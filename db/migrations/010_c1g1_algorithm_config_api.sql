-- Migration 010: C1G.1 — Camera / ROI / algorithm configuration API.
--
-- Extends the existing C1/R3 camera configuration tables without replacing
-- the Savant runtime topology. Existing columns and compatibility fields are
-- preserved.

ALTER TABLE cameras
    ADD COLUMN IF NOT EXISTS input_type TEXT NOT NULL DEFAULT 'rtsp',
    ADD COLUMN IF NOT EXISTS rtsp_transport TEXT NOT NULL DEFAULT 'tcp',
    ADD COLUMN IF NOT EXISTS fps_policy JSONB NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS alert_policy JSONB NOT NULL DEFAULT '{}'::jsonb;

CREATE INDEX IF NOT EXISTS cameras_source_id_idx ON cameras(source_id);

ALTER TABLE camera_zones
    ADD COLUMN IF NOT EXISTS zone_id TEXT,
    ADD COLUMN IF NOT EXISTS coordinate_space TEXT NOT NULL DEFAULT 'pixel',
    ADD COLUMN IF NOT EXISTS enabled BOOLEAN NOT NULL DEFAULT TRUE;

UPDATE camera_zones
SET zone_id = zone_name
WHERE zone_id IS NULL OR zone_id = '';

ALTER TABLE camera_zones
    ALTER COLUMN zone_id SET NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'camera_zones_camera_zone_id_unique'
    ) THEN
        ALTER TABLE camera_zones
            ADD CONSTRAINT camera_zones_camera_zone_id_unique UNIQUE (camera_id, zone_id);
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS camera_zones_camera_zone_id_idx
    ON camera_zones(camera_id, zone_id);

ALTER TABLE camera_rules
    ADD COLUMN IF NOT EXISTS rule_id TEXT,
    ADD COLUMN IF NOT EXISTS algorithm_id TEXT;

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

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'camera_rules_camera_rule_id_unique'
    ) THEN
        ALTER TABLE camera_rules
            ADD CONSTRAINT camera_rules_camera_rule_id_unique UNIQUE (camera_id, rule_id);
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS camera_rules_camera_rule_id_idx
    ON camera_rules(camera_id, rule_id);

CREATE INDEX IF NOT EXISTS camera_rules_camera_algorithm_id_idx
    ON camera_rules(camera_id, algorithm_id);
