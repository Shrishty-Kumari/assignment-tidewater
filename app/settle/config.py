import os


def _int(name, default):
    return int(os.getenv(name, str(default)))


def _float(name, default):
    return float(os.getenv(name, str(default)))

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql+psycopg://settle@localhost:5432/settle")
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
BANK_API_URL = os.getenv("BANK_API_URL", "http://bankmock:8080")

DB_POOL_SIZE = _int("DB_POOL_SIZE", 2)
DB_MAX_OVERFLOW = _int("DB_MAX_OVERFLOW", 0)
# fail fast when the pool is exhausted instead of holding the request for 30 s
DB_POOL_TIMEOUT = _float("DB_POOL_TIMEOUT", 2)
DB_POOL_RECYCLE = _int("DB_POOL_RECYCLE", 1800)
DB_CONNECT_TIMEOUT = _int("DB_CONNECT_TIMEOUT", 3)
DB_STATEMENT_TIMEOUT_MS = _int("DB_STATEMENT_TIMEOUT_MS", 5000)
REPORT_STATEMENT_TIMEOUT_MS = _int("REPORT_STATEMENT_TIMEOUT_MS", 15000)
DB_LOCK_TIMEOUT_MS = _int("DB_LOCK_TIMEOUT_MS", 2000)


LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

WORKER_CONCURRENCY = _int("WORKER_CONCURRENCY", 4)
WORKER_ID = os.getenv("WORKER_ID") or os.getenv("HOSTNAME", "worker-local")
WORKER_LEASE_SECONDS = _int("WORKER_LEASE_SECONDS", 30)
WORKER_SHUTDOWN_TIMEOUT = _int("WORKER_SHUTDOWN_TIMEOUT", 45)
WORKER_MAX_ATTEMPTS = _int("WORKER_MAX_ATTEMPTS", 8)
WORKER_RETRY_BASE_SECONDS = _float("WORKER_RETRY_BASE_SECONDS", 2)
WORKER_RETRY_MAX_SECONDS = _float("WORKER_RETRY_MAX_SECONDS", 300)
WORKER_METRICS_PORT = _int("WORKER_METRICS_PORT", 9100)
BANK_TIMEOUT_SECONDS = _float("BANK_TIMEOUT_SECONDS", 10)
RECONCILE_INTERVAL_SECONDS = _int("RECONCILE_INTERVAL_SECONDS", 60)
REPORT_SIMULATED_COST_SECONDS = _float("REPORT_SIMULATED_COST_SECONDS", 0)
