"""Migrations against a real Postgres, and N-1 compatibility.

Runs only when PG_TEST_URL is set (CI provides a postgres:15 service), e.g.
    PG_TEST_URL=postgresql://settle:test@localhost:55432/settle pytest tests/integration
The database is wiped first.
"""

import os
import threading
from pathlib import Path

import psycopg
import pytest

PG = os.getenv("PG_TEST_URL")
pytestmark = pytest.mark.skipif(not PG, reason="PG_TEST_URL not set")
MIGRATIONS = Path(__file__).resolve().parents[3] / "migrations"


@pytest.fixture(scope="module")
def migrated():
    with psycopg.connect(PG, autocommit=True) as c:
        c.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    os.environ["DATABASE_URL"] = PG.replace("postgresql://", "postgresql+psycopg://")
    os.environ["MIGRATIONS_DIR"] = str(MIGRATIONS)
    from settle import config, migrate

    config.DATABASE_URL = os.environ["DATABASE_URL"]
    migrate.MIGRATIONS_DIR = MIGRATIONS
    # production today: 0007 plus v1.7 data
    migrate.apply_file(MIGRATIONS / "0007_settlements_payouts.sql")
    with psycopg.connect(PG, autocommit=True) as c:
        c.execute("INSERT INTO merchants (name, bank_account) VALUES ('m', 'DE1')")
        c.execute("INSERT INTO settlements (merchant_id, settlement_date, amount_minor, currency) "
                  "SELECT 1, DATE '2026-01-01' + g, 100, 'EUR' FROM generate_series(0, 99) g")
        c.execute("INSERT INTO payouts (settlement_id, merchant_id, amount_minor, currency, status) "
                  "SELECT (g % 100) + 1, 1, 100, 'EUR', 'paid' FROM generate_series(1, 20000) g")
    assert migrate.run() == 0
    return migrate


def q(sql, *args):
    with psycopg.connect(PG, autocommit=True) as c:
        cur = c.execute(sql, args)
        return cur.fetchall() if cur.description else None


def test_backfill_complete(migrated):
    assert q("SELECT count(*) FROM payouts WHERE state IS DISTINCT FROM status") == [(0,)]


def test_rerun_is_noop(migrated):
    assert migrated.run() == 0


def test_v17_and_v19_run_side_by_side(migrated):
    """Rolling update: old pods write status, new pods write state, concurrently."""
    errors = []

    def v17():
        try:
            for _ in range(50):
                q("INSERT INTO payouts (settlement_id, merchant_id, amount_minor, currency) VALUES (1,1,1,'EUR')")
                q("UPDATE payouts SET status = 'paid' WHERE id = (SELECT max(id) FROM payouts)")
                q("SELECT s.id, p.status FROM settlements s LEFT JOIN payouts p ON p.settlement_id = s.id LIMIT 5")
        except Exception as e:  # pragma: no cover
            errors.append(e)

    def v19():
        try:
            for _ in range(50):
                q("INSERT INTO payouts (settlement_id, merchant_id, amount_minor, currency, state) VALUES (2,1,1,'EUR','queued')")
                q("UPDATE payouts SET state = 'sending', bank_idempotency_key = gen_random_uuid() "
                  "WHERE id = (SELECT max(id) FROM payouts WHERE settlement_id = 2)")
                q("SELECT s.id, p.state FROM settlements s LEFT JOIN payouts p ON p.settlement_id = s.id LIMIT 5")
        except Exception as e:  # pragma: no cover
            errors.append(e)

    threads = [threading.Thread(target=f) for f in (v17, v19)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors
    assert q("SELECT count(*) FROM payouts WHERE state IS DISTINCT FROM status") == [(0,)]


def test_rollback_to_v17_needs_no_schema_change(migrated):
    # after v1.9 wrote 'sending'/'paid' via state, v1.7 reads status and sees it
    q("UPDATE payouts SET state = 'paid' WHERE id = 1")
    assert q("SELECT status FROM payouts WHERE id = 1") == [("paid",)]


def test_idempotency_key_unique(migrated):
    q("UPDATE payouts SET bank_idempotency_key = '00000000-0000-0000-0000-000000000001' WHERE id = 2")
    with pytest.raises(psycopg.errors.UniqueViolation):
        q("UPDATE payouts SET bank_idempotency_key = '00000000-0000-0000-0000-000000000001' WHERE id = 3")


def test_indexes_valid_and_constraint_validated(migrated):
    rows = q("SELECT c.relname, i.indisvalid FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
             "WHERE c.relname IN ('idx_payouts_state', 'uq_payouts_bank_idempotency_key')")
    assert sorted(rows) == [("idx_payouts_state", True), ("uq_payouts_bank_idempotency_key", True)]
    assert q("SELECT convalidated FROM pg_constraint WHERE conname = 'payouts_state_not_null'") == [(True,)]
