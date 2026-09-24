-- 0007: settlements and payouts (current production schema, v1.7.x)
-- lint:allow baseline  applied in production before the lint rules existed
-- lint:allow index-not-concurrent  index is created together with its (empty) table
BEGIN;

CREATE TABLE IF NOT EXISTS merchants (
    id           bigserial PRIMARY KEY,
    name         text        NOT NULL,
    bank_account text        NOT NULL,
    created_at   timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS settlements (
    id              bigserial PRIMARY KEY,
    merchant_id     bigint      NOT NULL REFERENCES merchants (id),
    settlement_date date        NOT NULL,
    amount_minor    bigint      NOT NULL CHECK (amount_minor >= 0),
    currency        char(3)     NOT NULL,
    status          text        NOT NULL DEFAULT 'pending',
    created_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (merchant_id, settlement_date)
);

CREATE TABLE IF NOT EXISTS payouts (
    id            bigserial PRIMARY KEY,
    settlement_id bigint      NOT NULL REFERENCES settlements (id),
    merchant_id   bigint      NOT NULL REFERENCES merchants (id),
    amount_minor  bigint      NOT NULL,
    currency      char(3)     NOT NULL,
    status        text        NOT NULL DEFAULT 'queued',
    bank_ref      text,
    created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_payouts_settlement ON payouts (settlement_id);

CREATE TABLE IF NOT EXISTS schema_migrations (
    version    text PRIMARY KEY,
    applied_at timestamptz NOT NULL DEFAULT now()
);
INSERT INTO schema_migrations (version) VALUES ('0007') ON CONFLICT DO NOTHING;

COMMIT;
