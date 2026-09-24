from __future__ import annotations

import json
import logging
import random
import signal
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import redis
from prometheus_client import start_http_server
from sqlalchemy import exc as sa_exc
from sqlalchemy import text

from settle import __version__, bank, config, db, logs, metrics, queue

log = logging.getLogger("settle.worker")
ALIVE_FILE = Path("/tmp/settle-worker-alive")  # noqa: S108 - per-pod emptyDir, not shared

# Moves due jobs from the retry zset back onto the queue atomically.
PROMOTE_DUE = """
local due = redis.call('ZRANGEBYSCORE', KEYS[1], '-inf', ARGV[1], 'LIMIT', 0, 100)
for _, job in ipairs(due) do
  redis.call('ZREM', KEYS[1], job)
  redis.call('RPUSH', KEYS[2], job)
end
return #due
"""


class PermanentError(Exception):
    pass


# --- payout execution ---------------------------------------------------------
def claim(payout_id: int):
    """Claim the payout for sending. Returns (idempotency_key, None) or (None, current_state)."""
    with db.engine.begin() as conn:
        row = conn.execute(
            text(
                "UPDATE payouts SET state = 'sending', "
                "bank_idempotency_key = COALESCE(bank_idempotency_key, :key) "
                "WHERE id = :id AND state IN ('queued', 'sending') "
                "RETURNING bank_idempotency_key"
            ),
            {"id": payout_id, "key": str(uuid.uuid4())},
        ).first()
        if row:
            return row[0], None
        state = conn.execute(text("SELECT state FROM payouts WHERE id = :id"), {"id": payout_id}).scalar()
        return None, state


def mark_paid(payout_id: int, bank_ref: str):
    with db.engine.begin() as conn:
        conn.execute(
            text("UPDATE payouts SET state = 'paid', bank_ref = :ref WHERE id = :id AND state = 'sending'"),
            {"ref": bank_ref, "id": payout_id},
        )


def handle(job: dict) -> str:
    pid = job["payout_id"]
    key, state = claim(pid)
    if key is None:
        if state == "paid":
            log.info("payout already paid, acknowledging redelivered job")
            return "duplicate_skipped"
        raise PermanentError(f"payout {pid} is in state {state!r}, cannot send")
    result = bank.pay(
        job["merchant_id"], job["amount_minor"], job["currency"],
        reference=f"payout-{pid}", idempotency_key=key, request_id=job.get("request_id"),
    )
    replayed = bool(result.get("replayed"))
    metrics.BANK_CALLS.labels("replayed" if replayed else "ok").inc()
    log.info("bank payout accepted", extra={"bank_ref": result["bank_ref"], "replayed": replayed})
    mark_paid(pid, result["bank_ref"])
    if job.get("enqueued_at"):
        metrics.PAYOUT_LATENCY.observe(max(0.0, time.time() - job["enqueued_at"]))
    log.info("payout marked paid")
    return "paid"


def backoff(attempt: int) -> float:
    delay = min(config.WORKER_RETRY_BASE_SECONDS * 2 ** (attempt - 1), config.WORKER_RETRY_MAX_SECONDS)
    return delay * random.uniform(0.8, 1.2)


# --- worker --------------------------------------------------------------------
class Worker:
    def __init__(self, worker_id: str | None = None, r: redis.Redis | None = None):
        self.id = worker_id or config.WORKER_ID
        self.r = r or queue.client
        self.processing = queue.processing_key(self.id)
        self.stop = threading.Event()
        self.inflight = 0
        self._lock = threading.Lock()
        self._promote = self.r.register_script(PROMOTE_DUE)
        self._rl = logs.RateLimitedLogger(log)

    # ack / retry / dead-letter; each is one atomic Redis transaction
    def _ack(self, raw: str):
        self.r.lrem(self.processing, 1, raw)

    def _retry_or_dead(self, raw: str, job: dict, exc: Exception, retryable: bool):
        attempt = int(job.get("attempt", 0)) + 1
        job = {**job, "attempt": attempt, "last_error": str(exc)[:300]}
        pipe = self.r.pipeline(transaction=True)
        if retryable and attempt < config.WORKER_MAX_ATTEMPTS:
            delay = backoff(attempt)
            pipe.zadd(queue.RETRY, {json.dumps(job): time.time() + delay})
            outcome = "retried"
            log.warning("payout failed, will retry", extra={"attempt": attempt, "retry_in_s": round(delay, 1), "error": str(exc)[:300]})
        else:
            pipe.lpush(queue.DEAD, json.dumps(job))
            outcome = "dead_lettered"
            log.error("payout dead-lettered", extra={"attempt": attempt, "retryable": retryable, "error": str(exc)[:300]})
        pipe.lrem(self.processing, 1, raw)
        pipe.execute()
        metrics.JOBS.labels(outcome).inc()

    def process(self, raw: str):
        try:
            job = json.loads(raw)
        except ValueError:
            self.r.pipeline().lpush(queue.DEAD, raw).lrem(self.processing, 1, raw).execute()
            metrics.JOBS.labels("dead_lettered").inc()
            return
        rid_token = logs.request_id.set(job.get("request_id"))
        pid_token = logs.payout_id.set(job.get("payout_id"))
        try:
            outcome = handle(job)
            self._ack(raw)
            metrics.JOBS.labels(outcome).inc()
        except bank.BankError as exc:
            if not exc.retryable:
                metrics.BANK_CALLS.labels("rejected").inc()
            else:
                metrics.BANK_CALLS.labels("error").inc()
            self._retry_or_dead(raw, job, exc, exc.retryable)
        except PermanentError as exc:
            self._retry_or_dead(raw, job, exc, retryable=False)
        except (sa_exc.OperationalError, sa_exc.TimeoutError, sa_exc.DBAPIError, redis.RedisError) as exc:
            # safe to retry: the claim and the idempotency key make re-execution a no-op
            self._retry_or_dead(raw, job, exc, retryable=True)
        except Exception as exc:  # unknown: still idempotent, retry but bounded
            log.exception("unexpected error processing payout")
            self._retry_or_dead(raw, job, exc, retryable=True)
        finally:
            logs.request_id.reset(rid_token)
            logs.payout_id.reset(pid_token)

    def _done(self, _fut):
        with self._lock:
            self.inflight -= 1
            metrics.INFLIGHT.set(self.inflight)

    # housekeeping ------------------------------------------------------------
    def heartbeat(self):
        self.r.set(queue.heartbeat_key(self.id), __version__, ex=config.WORKER_LEASE_SECONDS)
        ALIVE_FILE.touch()

    def recover_dead_workers(self) -> int:
        """Re-queue jobs held by workers whose lease expired. Safe to run on every worker."""
        moved = 0
        for key in self.r.scan_iter(f"{queue.PROCESSING_PREFIX}*"):
            owner = key[len(queue.PROCESSING_PREFIX):]
            if owner == self.id or self.r.exists(queue.heartbeat_key(owner)):
                continue
            while self.r.lmove(key, queue.JOBS, "RIGHT", "RIGHT"):
                moved += 1
        if moved:
            metrics.RECOVERED.inc(moved)
            log.warning("recovered jobs from dead workers", extra={"jobs": moved})
        return moved

    def promote_due_retries(self) -> int:
        return int(self._promote(keys=[queue.RETRY, queue.JOBS], args=[time.time()]))

    def export_queue_metrics(self):
        metrics.QUEUE_DEPTH.labels("jobs").set(self.r.llen(queue.JOBS))
        metrics.QUEUE_DEPTH.labels("retry").set(self.r.zcard(queue.RETRY))
        metrics.QUEUE_DEPTH.labels("dead").set(self.r.llen(queue.DEAD))
        oldest = self.r.lindex(queue.JOBS, -1)
        age = 0.0
        if oldest:
            try:
                age = max(0.0, time.time() - float(json.loads(oldest).get("enqueued_at") or time.time()))
            except ValueError:
                pass
        metrics.OLDEST_JOB_AGE.set(age)

    def reconcile(self):
        """Settlement-correctness SLI: payouts the bank paid more than once, and stale payouts."""
        try:
            dups = httpx.get(f"{config.BANK_API_URL}/v1/ledger/duplicates", timeout=5).json()
            metrics.RECON_DUPLICATES.set(len(dups))
            with db.engine.connect() as conn:
                stale = conn.execute(text(
                    "SELECT count(*) FROM payouts WHERE state IN ('queued', 'sending') "
                    "AND created_at < now() - interval '15 minutes'")).scalar()
            metrics.STALE_PAYOUTS.set(stale or 0)
            metrics.RECON_LAST_SUCCESS.set(time.time())
            if dups:
                log.error("bank ledger shows duplicate payouts", extra={"references": list(dups)[:20]})
        except Exception as exc:
            self._rl.log(logging.WARNING, "reconcile", "reconciliation failed", error=str(exc)[:200])

    def _housekeeping_loop(self):
        last_recon = 0.0
        while not self.stop.wait(5):
            try:
                self.heartbeat()
                self.recover_dead_workers()
                self.promote_due_retries()
                self.export_queue_metrics()
                if time.monotonic() - last_recon > config.RECONCILE_INTERVAL_SECONDS:
                    last_recon = time.monotonic()
                    self.reconcile()
            except redis.RedisError as exc:
                self._rl.log(logging.WARNING, "housekeeping", "redis unavailable in housekeeping", error=str(exc))

    # main loop -------------------------------------------------------------------
    def run(self):
        signal.signal(signal.SIGTERM, self._on_signal)
        signal.signal(signal.SIGINT, self._on_signal)
        start_http_server(config.WORKER_METRICS_PORT)
        # keep the liveness file fresh while waiting: a DB outage must not
        # get the worker restarted in a loop
        db.wait_for_db(stop=self.stop.is_set, on_attempt=ALIVE_FILE.touch)
        self.heartbeat()
        log.info("settle-worker started", extra={"worker_id": self.id, "concurrency": config.WORKER_CONCURRENCY})
        threading.Thread(target=self._housekeeping_loop, name="housekeeping", daemon=True).start()
        pool = ThreadPoolExecutor(max_workers=config.WORKER_CONCURRENCY, thread_name_prefix="payout")
        delay = 0.5
        while not self.stop.is_set():
            ALIVE_FILE.touch()
            if self.inflight >= config.WORKER_CONCURRENCY:
                time.sleep(0.05)
                continue
            try:
                raw = self.r.blmove(queue.JOBS, self.processing, 1, "RIGHT", "LEFT")
                delay = 0.5
            except redis.RedisError as exc:
                self._rl.log(logging.WARNING, "fetch", "redis unavailable, backing off", error=str(exc), retry_in_s=delay)
                self.stop.wait(delay)
                delay = min(delay * 2, 30)
                continue
            if raw is None:
                continue
            with self._lock:
                self.inflight += 1
                metrics.INFLIGHT.set(self.inflight)
            pool.submit(self.process, raw).add_done_callback(self._done)
        self._drain(pool)

    def _on_signal(self, signum, _frame):
        log.info("shutdown requested, no longer fetching jobs", extra={"signal": signum, "inflight": self.inflight})
        self.stop.set()

    def _drain(self, pool: ThreadPoolExecutor):
        deadline = time.monotonic() + config.WORKER_SHUTDOWN_TIMEOUT
        while self.inflight and time.monotonic() < deadline:
            time.sleep(0.2)
        pool.shutdown(wait=False, cancel_futures=True)
        left = self.r.llen(self.processing)
        # Drop the lease right away so another worker recovers anything left
        # (safe: claim + idempotency key make re-execution a no-op).
        self.r.delete(queue.heartbeat_key(self.id))
        log.info("settle-worker stopped", extra={"unfinished_jobs": left, "clean": left == 0})


def check_alive(max_age: int = 60) -> int:
    """Exec liveness probe: the fetch loop must have ticked recently."""
    try:
        return 0 if time.time() - ALIVE_FILE.stat().st_mtime < max_age else 1
    except FileNotFoundError:
        return 1


def main():
    logs.setup("settle-worker")
    if "--check-alive" in sys.argv:
        sys.exit(check_alive())
    Worker().run()


if __name__ == "__main__":
    main()
