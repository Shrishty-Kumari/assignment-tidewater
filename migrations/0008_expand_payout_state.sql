-- 0008 (expand): add payouts.state and payouts.bank_idempotency_key next to
-- the legacy payouts.status column. Additive only; v1.7 keeps working.
--
-- * Nullable columns with no default are a catalog-only change (no rewrite).
-- * A trigger keeps status and state in sync in both directions, so v1.7
--   (writes status) and v1.9+ (writes state) can run side by side, and a
--   rollback to v1.7 needs no down-migration.
-- * lock_timeout: if we cannot get the brief ACCESS EXCLUSIVE lock within 3 s
--   we fail and retry later instead of queueing all traffic behind us.
SET lock_timeout = '3s';
SET statement_timeout = '30s';

ALTER TABLE payouts ADD COLUMN IF NOT EXISTS state text;
ALTER TABLE payouts ADD COLUMN IF NOT EXISTS bank_idempotency_key uuid;

CREATE OR REPLACE FUNCTION payouts_sync_state() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NEW.state IS NOT NULL THEN
            NEW.status := NEW.state;
        ELSE
            NEW.state := NEW.status;
        END IF;
    ELSIF NEW.state IS DISTINCT FROM OLD.state THEN
        NEW.status := NEW.state;
    ELSIF NEW.status IS DISTINCT FROM OLD.status THEN
        NEW.state := NEW.status;
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS payouts_sync_state ON payouts;
CREATE TRIGGER payouts_sync_state
    BEFORE INSERT OR UPDATE ON payouts
    FOR EACH ROW EXECUTE FUNCTION payouts_sync_state();
