-- migrate:no-transaction
-- 0009 (expand): indexes for the new columns, built without blocking writes.
--
-- CREATE INDEX CONCURRENTLY cannot run inside a transaction, hence the
-- header above. If a concurrent build fails it leaves an INVALID index; the
-- runner drops invalid indexes named here before retrying (settle.migrate).
SET lock_timeout = '3s';
SET statement_timeout = 0;

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_payouts_state ON payouts (state);

CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS uq_payouts_bank_idempotency_key
    ON payouts (bank_idempotency_key) WHERE bank_idempotency_key IS NOT NULL;
