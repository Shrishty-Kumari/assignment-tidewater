-- 0011 (expand): guarantee state is always set, without a blocking scan.
--
-- ADD CONSTRAINT ... NOT VALID only takes a brief lock. VALIDATE CONSTRAINT
-- scans under SHARE UPDATE EXCLUSIVE, which does not block reads or writes.
-- In the contract step, SET NOT NULL can use this valid CHECK and skip the
-- full-table scan (Postgres 12+).
SET lock_timeout = '3s';
SET statement_timeout = 0;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'payouts_state_not_null') THEN
        ALTER TABLE payouts ADD CONSTRAINT payouts_state_not_null CHECK (state IS NOT NULL) NOT VALID;
    END IF;
END;
$$;

ALTER TABLE payouts VALIDATE CONSTRAINT payouts_state_not_null;
