import fakeredis
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.pool import StaticPool

SCHEMA = [
    """CREATE TABLE settlements (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        merchant_id INTEGER NOT NULL,
        settlement_date DATE NOT NULL,
        amount_minor INTEGER NOT NULL,
        currency TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending'
    )""",
    """CREATE TABLE payouts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        settlement_id INTEGER NOT NULL,
        merchant_id INTEGER NOT NULL,
        amount_minor INTEGER NOT NULL,
        currency TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'queued',
        state TEXT,
        bank_idempotency_key TEXT,
        bank_ref TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""",
]


@pytest.fixture
def engine(monkeypatch):
    eng = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    with eng.begin() as conn:
        for stmt in SCHEMA:
            conn.execute(text(stmt))
    from settle import db

    monkeypatch.setattr(db, "engine", eng)
    return eng


@pytest.fixture
def redis_client(monkeypatch):
    client = fakeredis.FakeRedis(decode_responses=True)
    from settle import queue

    monkeypatch.setattr(queue, "client", client)
    return client
