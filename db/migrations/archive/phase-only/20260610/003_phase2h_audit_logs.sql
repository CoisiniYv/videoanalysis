-- Migration 003: Phase 2H audit_logs table
-- Tracks every event status mutation for operator accountability.

CREATE TABLE IF NOT EXISTS audit_logs (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    actor       TEXT NOT NULL,
    action      TEXT NOT NULL,
    entity_type TEXT NOT NULL DEFAULT 'event',
    entity_id   TEXT NOT NULL,
    payload     JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_audit_logs_entity ON audit_logs(entity_type, entity_id);
CREATE INDEX IF NOT EXISTS idx_audit_logs_actor ON audit_logs(actor);
CREATE INDEX IF NOT EXISTS idx_audit_logs_action ON audit_logs(action);
