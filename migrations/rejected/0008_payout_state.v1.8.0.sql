-- 0008: payout state machine + bank idempotency key (shipped in v1.8.0)
BEGIN;

ALTER TABLE payouts RENAME COLUMN status TO state;

ALTER TABLE payouts
    ADD COLUMN bank_idempotency_key uuid NOT NULL DEFAULT gen_random_uuid();

CREATE INDEX idx_payouts_state ON payouts (state);

INSERT INTO schema_migrations (version) VALUES ('0008');

COMMIT;
