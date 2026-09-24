-- rollback for 0008
BEGIN;
DROP INDEX IF EXISTS idx_payouts_state;
ALTER TABLE payouts DROP COLUMN bank_idempotency_key;
ALTER TABLE payouts RENAME COLUMN state TO status;
DELETE FROM schema_migrations WHERE version = '0008';
COMMIT;
