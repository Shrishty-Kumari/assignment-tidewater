import logging
import time

from sqlalchemy import create_engine, event, text

from settle import config

log = logging.getLogger("settle.db")


def _connect_args():
    if not config.DATABASE_URL.startswith("postgresql"):
        return {}
    return {
        "connect_timeout": config.DB_CONNECT_TIMEOUT,
        "application_name": config.WORKER_ID,
        "options": f"-c statement_timeout={config.DB_STATEMENT_TIMEOUT_MS}"
        f" -c lock_timeout={config.DB_LOCK_TIMEOUT_MS}"
        " -c idle_in_transaction_session_timeout=30000",
        "keepalives": 1,
        "keepalives_idle": 30,
        "keepalives_interval": 10,
        "keepalives_count": 3,
    }


def make_engine(url=None):
    url = url or config.DATABASE_URL
    kwargs = {}
    if url.startswith("postgresql"):
        kwargs = dict(
            pool_size=config.DB_POOL_SIZE,
            max_overflow=config.DB_MAX_OVERFLOW,
            pool_timeout=config.DB_POOL_TIMEOUT,
            pool_recycle=config.DB_POOL_RECYCLE,
            pool_pre_ping=True,
            connect_args=_connect_args(),
        )
    return create_engine(url, **kwargs)


engine = make_engine()


def pool_stats():
    pool = engine.pool
    try:
        return {"checked_out": pool.checkedout(), "size": pool.size(), "overflow": pool.overflow()}
    except AttributeError:
        return {"checked_out": 0, "size": 0, "overflow": 0}


def ping(timeout_s: float = 1.0) -> bool:
    """Cheap dependency probe for /healthz/deps and metrics. Never used by kubelet probes."""
    try:
        with engine.connect() as conn:
            conn.execute(text(f"SET LOCAL statement_timeout = {int(timeout_s * 1000)}"))
            conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


def wait_for_db(stop=lambda: False, max_backoff: float = 30.0, on_attempt=lambda: None):
    """Block until the DB answers, with capped exponential backoff.

    The old version retried in a tight loop with a full traceback per attempt:
    ~80 attempts/s per process (RCA CF4). This one sleeps 0.5, 1, 2 ... 30 s
    and logs one line per attempt.
    """
    delay, attempt = 0.5, 0
    while not stop():
        attempt += 1
        on_attempt()
        if ping(timeout_s=config.DB_CONNECT_TIMEOUT):
            if attempt > 1:
                log.info("database reachable", extra={"attempts": attempt})
            return True
        log.warning("database not reachable, backing off", extra={"attempt": attempt, "retry_in_s": delay})
        time.sleep(delay)
        delay = min(delay * 2, max_backoff)
    return False


@event.listens_for(engine, "checkout")
def _on_checkout(*_):
    from settle import metrics

    metrics.DB_POOL_CHECKED_OUT.inc()


@event.listens_for(engine, "checkin")
def _on_checkin(*_):
    from settle import metrics

    metrics.DB_POOL_CHECKED_OUT.dec()
