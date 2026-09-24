-- migrate:no-transaction
-- 0010 (backfill): copy status -> state for existing rows in small batches.
--
-- Each batch is its own transaction (COMMIT inside DO is allowed when the DO
-- block is not itself inside a transaction), so row locks are held for
-- milliseconds and autovacuum can keep up. SKIP LOCKED avoids fighting the
-- worker over rows it is updating right now. Re-runnable: it only touches
-- rows where state IS NULL.
SET lock_timeout = '3s';
SET statement_timeout = 0;

DO $$
DECLARE
    n integer;
BEGIN
    LOOP
        UPDATE payouts p
           SET state = p.status
         WHERE p.id IN (SELECT id FROM payouts
                         WHERE state IS NULL
                         LIMIT 5000
                         FOR UPDATE SKIP LOCKED);
        GET DIAGNOSTICS n = ROW_COUNT;
        COMMIT;
        EXIT WHEN n = 0;
        PERFORM pg_sleep(0.05);
    END LOOP;
END;
$$;
