"""Prometheus metrics for settle-api and settle-worker.

settle-api runs several gunicorn worker processes. prometheus_client's
multiprocess mode (PROMETHEUS_MULTIPROC_DIR, an emptyDir) aggregates them;
gunicorn.conf.py marks dead workers so their gauges disappear.
"""

import os

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
    multiprocess,
)

from settle import __version__

MULTIPROC = bool(os.getenv("PROMETHEUS_MULTIPROC_DIR"))
_gauge_mode = {"multiprocess_mode": "livesum"} if MULTIPROC else {}

BUILD_INFO = Gauge("settle_build_info", "Build information", ["version"],
                   **({"multiprocess_mode": "max"} if MULTIPROC else {}))
BUILD_INFO.labels(__version__).set(1)

HTTP_REQUESTS = Counter(
    "settle_http_requests_total", "HTTP requests", ["method", "route", "status", "version"]
)
HTTP_LATENCY = Histogram(
    "settle_http_request_duration_seconds", "HTTP request latency", ["method", "route", "version"],
    buckets=(0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 15),
)
DB_POOL_CHECKED_OUT = Gauge(
    "settle_db_pool_connections_checked_out", "DB connections checked out of the pool", **_gauge_mode
)
DB_ERRORS = Counter("settle_db_errors_total", "DB errors surfaced to callers", ["kind"])

# --- worker ---------------------------------------------------------------
JOBS = Counter("settle_jobs_total", "Payout jobs by outcome", ["outcome"])
BANK_CALLS = Counter("settle_bank_calls_total", "Calls to the bank payout API", ["result"])
PAYOUT_LATENCY = Histogram(
    "settle_payout_duration_seconds", "Time from job enqueue to payout recorded as paid",
    buckets=(1, 5, 15, 30, 60, 120, 300, 600, 900, 1800, 3600),
)
INFLIGHT = Gauge("settle_worker_inflight_jobs", "Jobs currently being processed")
QUEUE_DEPTH = Gauge("settle_queue_depth", "Jobs waiting", ["queue"])
OLDEST_JOB_AGE = Gauge("settle_queue_oldest_job_age_seconds", "Age of the oldest waiting job")
RECOVERED = Counter("settle_jobs_recovered_total", "Jobs recovered from dead workers")
RECON_DUPLICATES = Gauge(
    "settle_reconciliation_duplicate_payouts",
    "Payout references paid more than once according to the bank ledger",
)
RECON_LAST_SUCCESS = Gauge(
    "settle_reconciliation_last_success_timestamp_seconds", "Last successful reconciliation run"
)
STALE_PAYOUTS = Gauge(
    "settle_payouts_stale", "Payouts queued/sending for longer than 15 minutes"
)


def render():
    if MULTIPROC:
        registry = CollectorRegistry()
        multiprocess.MultiProcessCollector(registry)
        return generate_latest(registry), CONTENT_TYPE_LATEST
    return generate_latest(), CONTENT_TYPE_LATEST
