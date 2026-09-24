"""The guarantees that were missing on 14 Aug (RCA RC3)."""

import json
import time

import pytest
from sqlalchemy import text

from settle import bank, queue, worker


class FakeBank:
    """Behaves like bankmock: pays once per Idempotency-Key."""

    def __init__(self):
        self.ledger, self.by_key, self.calls = [], {}, 0

    def pay(self, merchant_id, amount_minor, currency, reference, idempotency_key, request_id=None):
        self.calls += 1
        if idempotency_key in self.by_key:
            return {**self.by_key[idempotency_key], "replayed": True}
        entry = {"bank_ref": f"BNK-{len(self.ledger) + 1}", "reference": reference}
        self.ledger.append(entry)
        self.by_key[idempotency_key] = entry
        return {**entry, "replayed": False}


@pytest.fixture
def fake_bank(monkeypatch):
    b = FakeBank()
    monkeypatch.setattr(worker.bank, "pay", b.pay)
    return b


def seed(engine, n=1):
    with engine.begin() as conn:
        for _ in range(n):
            conn.execute(text(
                "INSERT INTO payouts (settlement_id, merchant_id, amount_minor, currency, status, state) "
                "VALUES (1, 7, 1250, 'EUR', 'queued', 'queued')"))


def job(pid=1, **kw):
    return json.dumps({"payout_id": pid, "merchant_id": 7, "amount_minor": 1250, "currency": "EUR",
                       "enqueued_at": time.time(), "attempt": 0, **kw})


def state(engine, pid=1):
    with engine.connect() as conn:
        return conn.execute(text("SELECT state, bank_idempotency_key FROM payouts WHERE id = :id"),
                            {"id": pid}).one()


def test_job_pays_once_and_is_acknowledged(engine, redis_client, fake_bank):
    seed(engine)
    w = worker.Worker("w1", redis_client)
    raw = job()
    redis_client.lpush(w.processing, raw)
    w.process(raw)
    assert state(engine).state == "paid"
    assert len(fake_bank.ledger) == 1
    assert redis_client.llen(w.processing) == 0


def test_redelivered_job_for_paid_payout_does_not_call_bank(engine, redis_client, fake_bank):
    seed(engine)
    w = worker.Worker("w1", redis_client)
    raw = job()
    w.process(raw)
    w.process(raw)  # e.g. recovered from a dead worker after it had finished
    assert fake_bank.calls == 1
    assert len(fake_bank.ledger) == 1


def test_crash_after_bank_call_is_replayed_not_paid_twice(engine, redis_client, fake_bank, monkeypatch):
    """The exact 14 Aug sequence: bank paid, then the worker died before recording it."""
    seed(engine)
    w = worker.Worker("w1", redis_client)
    raw = job()
    real_mark_paid = worker.mark_paid
    monkeypatch.setattr(worker, "mark_paid", lambda *a: (_ for _ in ()).throw(SystemExit("killed")))
    with pytest.raises(SystemExit):
        worker.handle(json.loads(raw))
    assert state(engine).state == "sending"
    monkeypatch.setattr(worker, "mark_paid", real_mark_paid)
    w.process(raw)  # redelivery by another worker
    assert state(engine).state == "paid"
    assert fake_bank.calls == 2
    assert len(fake_bank.ledger) == 1, "second call must be a replay of the first"


def test_only_dead_workers_jobs_are_recovered(engine, redis_client):
    alive, dead = worker.Worker("alive", redis_client), worker.Worker("dead", redis_client)
    alive.heartbeat()
    redis_client.lpush(alive.processing, job(1))
    redis_client.lpush(dead.processing, job(2))  # no heartbeat -> lease expired
    rescuer = worker.Worker("rescuer", redis_client)
    assert rescuer.recover_dead_workers() == 1
    assert redis_client.llen(alive.processing) == 1, "must not steal work from a live worker"
    assert json.loads(redis_client.lindex(queue.JOBS, 0))["payout_id"] == 2


def test_retryable_failure_is_scheduled_with_backoff_then_dead_lettered(engine, redis_client, monkeypatch):
    seed(engine)
    monkeypatch.setattr(worker.config, "WORKER_MAX_ATTEMPTS", 3)

    def unavailable(*a, **kw):
        raise bank.BankError("bank returned 503", retryable=True)

    monkeypatch.setattr(worker.bank, "pay", unavailable)
    w = worker.Worker("w1", redis_client)
    raw = job()
    w.process(raw)
    (retry_raw, due), = redis_client.zrange(queue.RETRY, 0, -1, withscores=True)
    assert json.loads(retry_raw)["attempt"] == 1 and due > time.time()
    w.process(json.dumps({**json.loads(retry_raw)}))
    w.process(json.dumps({**json.loads(retry_raw), "attempt": 2}))
    assert redis_client.llen(queue.DEAD) == 1
    assert redis_client.llen(w.processing) == 0


def test_rejected_payout_is_dead_lettered_immediately(engine, redis_client, monkeypatch):
    seed(engine)
    monkeypatch.setattr(worker.bank, "pay",
                        lambda *a, **kw: (_ for _ in ()).throw(bank.BankError("400", retryable=False)))
    w = worker.Worker("w1", redis_client)
    w.process(job())
    assert redis_client.llen(queue.DEAD) == 1
    assert redis_client.zcard(queue.RETRY) == 0


def test_due_retries_are_promoted(redis_client):
    w = worker.Worker("w1", redis_client)
    redis_client.zadd(queue.RETRY, {job(1): time.time() - 1, job(2): time.time() + 3600})
    assert w.promote_due_retries() == 1
    assert redis_client.llen(queue.JOBS) == 1


def test_backoff_is_capped():
    assert worker.backoff(1) <= worker.config.WORKER_RETRY_BASE_SECONDS * 1.2
    assert worker.backoff(50) <= worker.config.WORKER_RETRY_MAX_SECONDS * 1.2


def test_drain_waits_for_inflight_and_releases_lease(redis_client, monkeypatch):
    w = worker.Worker("w1", redis_client)
    w.heartbeat()
    w.inflight = 1

    class Pool:
        def shutdown(self, **kw):
            pass

    import threading
    threading.Timer(0.3, lambda: setattr(w, "inflight", 0)).start()
    t0 = time.monotonic()
    w._drain(Pool())
    assert time.monotonic() - t0 >= 0.25, "must wait for the in-flight job"
    assert not redis_client.exists(queue.heartbeat_key("w1"))
