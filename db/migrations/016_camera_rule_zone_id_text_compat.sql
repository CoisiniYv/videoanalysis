-- Migration 016: camera rule zone_id text compatibility.
--
-- Long-lived databases initialized from the early 001 schema keep
-- camera_rules.zone_id as UUID with an FK to camera_zones.id. The 8090
-- operator API binds algorithm rules to camera_zones.zone_id text identifiers
-- such as lab_full_frame, so camera_rules.zone_id must be text too.

ALTER TABLE camera_rules
    DROP CONSTRAINT IF EXISTS camera_rules_zone_id_fkey;

ALTER TABLE camera_rules
    ALTER COLUMN zone_id TYPE TEXT USING zone_id::text;

UPDATE camera_rules AS r
SET zone_id = z.zone_id
FROM camera_zones AS z
WHERE r.zone_id = z.id::text
  AND r.camera_id = z.camera_id
  AND z.zone_id IS NOT NULL
  AND z.zone_id <> '';
