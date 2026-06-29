-- Migration 021: Qdrant gallery sync outbox.
--
-- PostgreSQL remains the source of truth for registered people and gallery
-- embeddings. Qdrant is a derived serving index, rebuilt from these rows.

CREATE TABLE IF NOT EXISTS gallery_vector_sync_outbox (
    id BIGSERIAL PRIMARY KEY,
    gallery_embedding_id BIGINT NOT NULL,
    operation TEXT NOT NULL CHECK (operation IN ('upsert', 'delete')),
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'processing', 'completed', 'retry', 'failed', 'poisoned')),
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    processed_at TIMESTAMPTZ,
    claimed_at TIMESTAMPTZ,
    claimed_by TEXT,
    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    reason TEXT
);

CREATE INDEX IF NOT EXISTS idx_gallery_vector_sync_outbox_status_created
    ON gallery_vector_sync_outbox (status, created_at);

CREATE INDEX IF NOT EXISTS idx_gallery_vector_sync_outbox_retry_order
    ON gallery_vector_sync_outbox (status, next_attempt_at, created_at);

CREATE INDEX IF NOT EXISTS idx_gallery_vector_sync_outbox_gallery_recent
    ON gallery_vector_sync_outbox (gallery_embedding_id, created_at DESC);

CREATE OR REPLACE FUNCTION gallery_vector_sync_enqueue(
    p_gallery_embedding_id BIGINT,
    p_operation TEXT,
    p_reason TEXT DEFAULT NULL
) RETURNS VOID
LANGUAGE plpgsql
AS $$
BEGIN
    IF p_gallery_embedding_id IS NULL THEN
        RETURN;
    END IF;
    IF p_operation NOT IN ('upsert', 'delete') THEN
        RAISE EXCEPTION 'invalid gallery vector sync operation: %', p_operation;
    END IF;
    INSERT INTO gallery_vector_sync_outbox (
        gallery_embedding_id,
        operation,
        reason
    )
    VALUES (
        p_gallery_embedding_id,
        p_operation,
        p_reason
    );
END;
$$;

CREATE OR REPLACE FUNCTION gallery_vector_sync_person_gallery_trigger()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
DECLARE
    v_gallery_id BIGINT;
    v_operation TEXT;
BEGIN
    IF TG_OP = 'DELETE' THEN
        v_gallery_id := OLD.id;
        v_operation := 'delete';
    ELSE
        v_gallery_id := NEW.id;
        IF NEW.is_active = true AND NEW.embedding IS NOT NULL THEN
            v_operation := 'upsert';
        ELSE
            v_operation := 'delete';
        END IF;
    END IF;

    PERFORM gallery_vector_sync_enqueue(
        v_gallery_id,
        v_operation,
        'person_gallery_embeddings_' || lower(TG_OP)
    );

    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_gallery_vector_sync_person_gallery
    ON person_gallery_embeddings;

CREATE TRIGGER trg_gallery_vector_sync_person_gallery
AFTER INSERT OR UPDATE OR DELETE
ON person_gallery_embeddings
FOR EACH ROW
EXECUTE FUNCTION gallery_vector_sync_person_gallery_trigger();

CREATE OR REPLACE FUNCTION gallery_vector_sync_person_trigger()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
DECLARE
    v_operation TEXT;
    v_row RECORD;
BEGIN
    IF TG_OP <> 'UPDATE' THEN
        RETURN NEW;
    END IF;

    IF OLD.is_active IS NOT DISTINCT FROM NEW.is_active
       AND OLD.name IS NOT DISTINCT FROM NEW.name
       AND OLD.external_person_id IS NOT DISTINCT FROM NEW.external_person_id THEN
        RETURN NEW;
    END IF;

    FOR v_row IN
        SELECT id, is_active, embedding
        FROM person_gallery_embeddings
        WHERE person_id = NEW.id
    LOOP
        IF NEW.is_active = true
           AND v_row.is_active = true
           AND v_row.embedding IS NOT NULL THEN
            v_operation := 'upsert';
        ELSE
            v_operation := 'delete';
        END IF;
        PERFORM gallery_vector_sync_enqueue(
            v_row.id,
            v_operation,
            'persons_update'
        );
    END LOOP;

    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_gallery_vector_sync_person
    ON persons;

CREATE TRIGGER trg_gallery_vector_sync_person
AFTER UPDATE OF is_active, name, external_person_id
ON persons
FOR EACH ROW
EXECUTE FUNCTION gallery_vector_sync_person_trigger();

COMMENT ON TABLE gallery_vector_sync_outbox IS
    'Transactional outbox for syncing canonical PostgreSQL gallery embeddings into Qdrant.';

COMMENT ON FUNCTION gallery_vector_sync_enqueue(BIGINT, TEXT, TEXT) IS
    'Enqueue a rebuildable Qdrant gallery vector sync operation in the current transaction.';
