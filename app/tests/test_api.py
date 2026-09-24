import json

from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from settle import db, queue
from settle.api import app

client = TestClient(app)


def test_create_and_get_settlement(engine, redis_client):
    r = client.post(
        "/settlements",
        json={"merchant_id": 7, "settlement_date": "2026-08-14", "amount_minor": 1250},
    )
    assert r.status_code == 201
    sid = r.json()["id"]
    r = client.get(f"/settlements/{sid}")
    assert r.status_code == 200
    assert r.json()["status"] == "pending"


def test_execute_enqueues_exactly_one_job_with_correlation_id(engine, redis_client):
    sid = client.post(
        "/settlements",
        json={"merchant_id": 7, "settlement_date": "2026-08-14", "amount_minor": 1250},
    ).json()["id"]
    r = client.post(f"/settlements/{sid}/execute", headers={"X-Request-ID": "req-abc"})
    assert r.status_code == 202
    assert r.headers["X-Request-ID"] == "req-abc"
    assert client.post(f"/settlements/{sid}/execute").status_code == 409
    jobs = redis_client.lrange(queue.JOBS, 0, -1)
    assert len(jobs) == 1
    job = json.loads(jobs[0])
    assert job["merchant_id"] == 7
    assert job["request_id"] == "req-abc"
    assert job["attempt"] == 0 and job["enqueued_at"] > 0


def test_unknown_settlement_is_404(engine, redis_client):
    assert client.get("/settlements/999").status_code == 404


def test_probes_do_not_depend_on_the_database(monkeypatch):
    # A database that cannot be reached must not fail liveness or readiness:
    # on 14 Aug that restarted every pod at once.
    monkeypatch.setattr(db, "engine", create_engine("postgresql+psycopg://x@127.0.0.1:1/x",
                                                    connect_args={"connect_timeout": 1}))
    assert client.get("/livez").status_code == 200
    assert client.get("/readyz").status_code == 200
    assert client.get("/healthz").status_code == 200


def test_database_errors_return_503_with_retry_after(monkeypatch):
    monkeypatch.setattr(db, "engine", create_engine("postgresql+psycopg://x@127.0.0.1:1/x",
                                                    connect_args={"connect_timeout": 1}))
    r = client.get("/settlements")
    assert r.status_code == 503
    assert r.headers["Retry-After"] == "2"


def test_metrics_exposed(engine, redis_client):
    client.get("/settlements")
    body = client.get("/metrics").text
    assert "settle_http_requests_total" in body
    assert "settle_build_info" in body
