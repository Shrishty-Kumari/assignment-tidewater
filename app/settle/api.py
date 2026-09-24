import datetime as dt
import json
import logging
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel
from sqlalchemy import exc as sa_exc
from sqlalchemy import text

from settle import __version__, config, db, logs, metrics, queue

logs.setup("settle-api")
log = logging.getLogger("settle.api")

_state = {"draining": False}


class SettlementIn(BaseModel):
    merchant_id: int
    settlement_date: dt.date
    amount_minor: int
    currency: str = "EUR"

@asynccontextmanager
async def lifespan(_app):
    log.info("settle-api started", extra={"pool": db.pool_stats()})
    yield
    _state["draining"] = True
    log.info("settle-api shutting down, draining in-flight requests")
    db.engine.dispose()


app = FastAPI(title="settle-api", version=__version__, lifespan=lifespan)


@app.middleware("http")
async def observe(request: Request, call_next):
    rid = request.headers.get("x-request-id") or uuid.uuid4().hex
    token = logs.request_id.set(rid)
    start = time.perf_counter()
    status = 500
    try:
        response = await call_next(request)
        status = response.status_code
        response.headers["X-Request-ID"] = rid
        return response
    finally:
        elapsed = time.perf_counter() - start
        route = getattr(request.scope.get("route"), "path", "unmatched")
        if route not in ("/livez", "/readyz", "/metrics"):
            metrics.HTTP_REQUESTS.labels(request.method, route, str(status), __version__).inc()
            metrics.HTTP_LATENCY.labels(request.method, route, __version__).observe(elapsed)
            log.info(
                "request",
                extra={"method": request.method, "route": route, "path": request.url.path,
                       "status": status, "duration_ms": round(elapsed * 1000, 1)},
            )
        logs.request_id.reset(token)


@app.exception_handler(sa_exc.TimeoutError)
@app.exception_handler(sa_exc.OperationalError)
async def db_unavailable(request: Request, exc: Exception):
    kind = "pool_timeout" if isinstance(exc, sa_exc.TimeoutError) else "operational"
    if "canceling statement due to" in str(exc):
        kind = "statement_timeout"
    metrics.DB_ERRORS.labels(kind).inc()
    log.warning("database unavailable", extra={"kind": kind, "error": str(exc).splitlines()[0][:300]})
    return JSONResponse({"error": "database unavailable", "kind": kind}, status_code=503,
                        headers={"Retry-After": "2"})


@app.get("/livez")
def livez():
    """Liveness: is this process able to answer at all? No dependencies, ever."""
    return {"status": "ok"}


@app.get("/readyz")
def readyz():
    """Readiness: should this pod receive traffic? Local conditions only.

    Postgres health is deliberately NOT checked here: it is shared by every
    pod, so a slow database would mark all pods unready at once and turn a
    partial degradation into a total outage. DB health is exported as metrics
    and alerted on instead.
    """
    if _state["draining"]:
        return JSONResponse({"status": "draining"}, status_code=503)
    return {"status": "ready", "version": __version__}


@app.get("/healthz")
def healthz():
    """Kept for old clients; identical to /livez."""
    return livez()


@app.get("/healthz/deps")
def healthz_deps():
    """Human/diagnostic view of dependencies. Not used by kubelet probes."""
    deps = {"postgres": db.ping(), "redis": queue.ping(), "pool": db.pool_stats()}
    ok = deps["postgres"] and deps["redis"]
    return JSONResponse({"status": "ok" if ok else "degraded", **deps}, status_code=200 if ok else 503)


@app.get("/metrics")
def metrics_endpoint():
    body, ctype = metrics.render()
    return Response(body, media_type=ctype)


@app.get("/settlements")
def list_settlements(settlement_date: dt.date | None = None, limit: int = 50):
    sql = """
        SELECT s.id, s.merchant_id, s.settlement_date, s.amount_minor, s.currency,
               s.status, p.state AS payout_state
          FROM settlements s
          LEFT JOIN payouts p ON p.settlement_id = s.id
         WHERE (CAST(:d AS date) IS NULL OR s.settlement_date = CAST(:d AS date))
         ORDER BY s.id DESC
         LIMIT :limit
    """
    with db.engine.connect() as conn:
        rows = conn.execute(text(sql), {"d": settlement_date, "limit": min(limit, 500)}).mappings()
        return [dict(r) for r in rows]


@app.get("/settlements/{settlement_id}")
def get_settlement(settlement_id: int):
    sql = """
        SELECT s.id, s.merchant_id, s.settlement_date, s.amount_minor, s.currency,
               s.status, p.id AS payout_id, p.state AS payout_state, p.bank_ref
          FROM settlements s
          LEFT JOIN payouts p ON p.settlement_id = s.id
         WHERE s.id = :id
    """
    with db.engine.connect() as conn:
        row = conn.execute(text(sql), {"id": settlement_id}).mappings().first()
    if row is None:
        raise HTTPException(404, "settlement not found")
    return dict(row)


@app.post("/settlements", status_code=201)
def create_settlement(body: SettlementIn):
    sql = """
        INSERT INTO settlements (merchant_id, settlement_date, amount_minor, currency, status)
        VALUES (:merchant_id, :settlement_date, :amount_minor, :currency, 'pending')
        RETURNING id
    """
    with db.engine.begin() as conn:
        new_id = conn.execute(text(sql), body.model_dump()).scalar_one()
    return {"id": new_id, "status": "pending"}


@app.post("/settlements/{settlement_id}/execute", status_code=202)
def execute_settlement(settlement_id: int):
    with db.engine.begin() as conn:
        s = conn.execute(
            text(
                "UPDATE settlements SET status = 'executing' "
                "WHERE id = :id AND status = 'pending' "
                "RETURNING id, merchant_id, amount_minor, currency"
            ),
            {"id": settlement_id},
        ).mappings().first()
        if s is None:
            raise HTTPException(409, "settlement is not pending")
        payout_id = conn.execute(
            text(
                "INSERT INTO payouts (settlement_id, merchant_id, amount_minor, currency, state) "
                "VALUES (:id, :merchant_id, :amount_minor, :currency, 'queued') RETURNING id"
            ),
            dict(s),
        ).scalar_one()
    job = {
        "payout_id": payout_id,
        "merchant_id": s["merchant_id"],
        "amount_minor": s["amount_minor"],
        "currency": s["currency"],
        "request_id": logs.request_id.get(),
        "enqueued_at": time.time(),
        "attempt": 0,
    }
    queue.client.lpush(queue.JOBS, json.dumps(job))
    return {"payout_id": payout_id, "state": "queued"}


@app.get("/reports/daily")
def daily_report(date: dt.date | None = None):
    date = date or dt.date.today()
    if config.REPORT_SIMULATED_COST_SECONDS:
        time.sleep(config.REPORT_SIMULATED_COST_SECONDS)
    sql = """
        SELECT s.currency, count(*) AS settlements, sum(s.amount_minor) AS total_minor,
               count(*) FILTER (WHERE p.state = 'paid') AS paid
          FROM settlements s
          LEFT JOIN payouts p ON p.settlement_id = s.id
         WHERE s.settlement_date = :d
         GROUP BY s.currency
    """
    with db.engine.begin() as conn:
        if config.DATABASE_URL.startswith("postgresql"):
            # the report is known to be slow; give it its own, larger budget
            conn.execute(text(f"SET LOCAL statement_timeout = {config.REPORT_STATEMENT_TIMEOUT_MS}"))
        rows = conn.execute(text(sql), {"d": date}).mappings()
        return {"date": str(date), "totals": [dict(r) for r in rows]}
