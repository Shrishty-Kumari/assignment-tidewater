"""Forward-only migration runner.

    python -m settle.migrate            # apply pending migrations
    python -m settle.migrate --status   # list applied / pending, exit 3 if pending

Rules (see docs/MIGRATIONS.md):
* only top-level migrations/NNNN_*.sql are applied, in order; rejected/ and
  contract/ are never picked up automatically;
* there are no down-migrations: every migration must be compatible with the
  previous app version, so rolling back the app never touches the schema;
* one runner at a time (advisory lock);
* files starting with "-- migrate:no-transaction" run statement by statement
  in autocommit (needed for CREATE INDEX CONCURRENTLY and batched backfills);
* a lock_timeout failure (SQLSTATE 55P03) is retried with backoff rather than
  waiting in the lock queue and blocking production traffic.
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
import time
from pathlib import Path

import psycopg

from settle import config, logs

log = logging.getLogger("settle.migrate")

ADVISORY_LOCK_ID = 7_331_008
NO_TX = "-- migrate:no-transaction"
LOCK_NOT_AVAILABLE = "55P03"
MIGRATIONS_DIR = Path(os.getenv("MIGRATIONS_DIR", "/app/migrations"))
# indexes that are built CONCURRENTLY; if a build failed they are INVALID and
# must be dropped before retrying
CONCURRENT_INDEXES = ("idx_payouts_state", "uq_payouts_bank_idempotency_key")


def dsn() -> str:
    return config.DATABASE_URL.replace("postgresql+psycopg://", "postgresql://")


def split_statements(sql: str) -> list[str]:
    """Split on semicolons outside quotes, dollar-quoted bodies and comments."""
    out, buf, i, n = [], [], 0, len(sql)
    dollar = None
    while i < n:
        c = sql[i]
        if dollar:
            if sql.startswith(dollar, i):
                buf.append(dollar)
                i += len(dollar)
                dollar = None
                continue
        elif sql.startswith("--", i):
            j = sql.find("\n", i)
            i = n if j == -1 else j + 1
            continue
        elif c == "'":
            j = sql.find("'", i + 1)
            j = n - 1 if j == -1 else j
            buf.append(sql[i : j + 1])
            i = j + 1
            continue
        elif c == "$":
            m = re.match(r"\$[A-Za-z_]*\$", sql[i:])
            if m:
                dollar = m.group(0)
                buf.append(dollar)
                i += len(dollar)
                continue
        elif c == ";":
            stmt = "".join(buf).strip()
            if stmt:
                out.append(stmt)
            buf = []
            i += 1
            continue
        buf.append(c)
        i += 1
    tail = "".join(buf).strip()
    if tail:
        out.append(tail)
    return out


def pending(conn, files):
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        " version text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
    )
    applied = {r[0] for r in conn.execute("SELECT version FROM schema_migrations")}
    return [f for f in files if f.name[:4] not in applied], applied


def drop_invalid_indexes(conn):
    rows = conn.execute(
        "SELECT c.relname FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
        "WHERE NOT i.indisvalid AND c.relname = ANY(%s)",
        (list(CONCURRENT_INDEXES),),
    ).fetchall()
    for (name,) in rows:
        log.warning("dropping invalid index left by a failed concurrent build", extra={"index": name})
        conn.execute(f'DROP INDEX CONCURRENTLY IF EXISTS "{name}"')


def apply_file(path: Path):
    sql = path.read_text()
    version = path.name[:4]
    if sql.lstrip().startswith(NO_TX):
        with psycopg.connect(dsn(), autocommit=True) as conn:
            drop_invalid_indexes(conn)
            for stmt in split_statements(sql):
                conn.execute(stmt)
            conn.execute(
                "INSERT INTO schema_migrations (version) VALUES (%s) ON CONFLICT DO NOTHING",
                (version,),
            )
    else:
        with psycopg.connect(dsn()) as conn:
            with conn.transaction():
                for stmt in split_statements(sql):
                    if stmt.upper() in ("BEGIN", "COMMIT"):
                        continue
                    conn.execute(stmt)
                conn.execute(
                    "INSERT INTO schema_migrations (version) VALUES (%s) ON CONFLICT DO NOTHING",
                    (version,),
                )


def connect_with_retry(deadline_s: float = 90):
    """The DB (or the network policy allowing us to reach it) may need a moment."""
    delay, deadline = 1.0, time.monotonic() + deadline_s
    while True:
        try:
            return psycopg.connect(dsn(), autocommit=True, connect_timeout=5)
        except psycopg.OperationalError as exc:
            if time.monotonic() + delay > deadline:
                raise
            log.warning("database not reachable yet, retrying", extra={"retry_in_s": delay, "error": str(exc)[:200]})
            time.sleep(delay)
            delay = min(delay * 2, 15)


def run(status_only: bool = False, attempts: int = 5) -> int:
    files = sorted(p for p in MIGRATIONS_DIR.glob("[0-9][0-9][0-9][0-9]_*.sql"))
    with connect_with_retry() as lock_conn:
        todo, applied = pending(lock_conn, files)
        if status_only:
            for f in files:
                print(f"{'applied' if f.name[:4] in applied else 'PENDING':8} {f.name}")
            return 3 if todo else 0
        deadline = time.monotonic() + 120
        while not lock_conn.execute("SELECT pg_try_advisory_lock(%s)", (ADVISORY_LOCK_ID,)).fetchone()[0]:
            if time.monotonic() > deadline:
                log.error("another migration runner holds the lock")
                return 1
            time.sleep(2)
        try:
            todo, _ = pending(lock_conn, files)
            for f in todo:
                for attempt in range(1, attempts + 1):
                    t0 = time.monotonic()
                    try:
                        apply_file(f)
                        log.info("migration applied", extra={"migration": f.name, "seconds": round(time.monotonic() - t0, 2)})
                        break
                    except psycopg.Error as exc:
                        if getattr(exc, "sqlstate", None) == LOCK_NOT_AVAILABLE and attempt < attempts:
                            delay = 2**attempt
                            log.warning("lock_timeout, retrying", extra={"migration": f.name, "attempt": attempt, "retry_in_s": delay})
                            time.sleep(delay)
                            continue
                        log.error("migration failed", extra={"migration": f.name, "error": str(exc)})
                        return 1
            if not todo:
                log.info("schema up to date")
            return 0
        finally:
            lock_conn.execute("SELECT pg_advisory_unlock(%s)", (ADVISORY_LOCK_ID,))


def main():
    logs.setup("settle-migrate")
    ap = argparse.ArgumentParser()
    ap.add_argument("--status", action="store_true")
    args = ap.parse_args()
    sys.exit(run(status_only=args.status))


if __name__ == "__main__":
    main()
