# Schema changes without downtime or down-migrations

## The rule

> Every migration must work with **both** the app version currently running
> and the one being deployed. Rolling back the app never touches the schema.

This means there are no down-migrations. The runner (`python -m settle.migrate`)
only moves forward. A bad release is fixed by rolling the *app* back, because
the schema it needs is still there.

## What 0008 should have been

v1.8.0 wanted two things: `payouts.status` renamed to `state`, and a
`bank_idempotency_key` per payout. The rejected migration did both in one
locking transaction (kept in `migrations/rejected/` for the record, never run). The redesign splits
the change into **expand → migrate → contract** across releases:

| File | Phase | What it does | Locks taken | Safe with |
|---|---|---|---|---|
| `0008_expand_payout_state.sql` | expand | Adds nullable `state` and `bank_idempotency_key` columns (catalog-only). Adds a trigger that syncs `status` and `state` in both directions. | `ACCESS EXCLUSIVE` for milliseconds, `lock_timeout = 3s` | v1.7 and v1.9 |
| `0009_index_payout_state.sql` | expand | `CREATE INDEX CONCURRENTLY` on `state` and a partial unique index on `bank_idempotency_key` | `SHARE UPDATE EXCLUSIVE` (reads and writes continue) | v1.7 and v1.9 |
| `0010_backfill_payout_state.sql` | migrate | `state = status` in batches of 5,000, one transaction per batch, `SKIP LOCKED` | row locks, milliseconds each | v1.7 and v1.9 |
| `0011_validate_payout_state.sql` | migrate | `CHECK (state IS NOT NULL) NOT VALID`, then `VALIDATE` | brief lock, then `SHARE UPDATE EXCLUSIVE` scan | v1.7 and v1.9 |
| `contract/0012_contract_drop_payout_status.sql` | contract | Drops the trigger, `SET NOT NULL` (uses the validated check, no scan), sets the default, drops `status` | brief | **v1.9+ only** |

`bank_idempotency_key` is intentionally left nullable. The worker assigns
it when it claims a payout (`UPDATE … SET bank_idempotency_key =
COALESCE(bank_idempotency_key, …)`). Historical payouts never need one.
There's no volatile default, so there's no table rewrite.

## Release sequence

```
 prod today: schema 0007, app v1.7.3
      │
      ▼
 R1  schema 0008–0011 (expand + backfill)      app v1.7.3 unchanged
      │   rollback: nothing to do; the change is additive and the trigger keeps
      │   state correct for v1.7 writes
      ▼
 R2  app v1.9.0: reads/writes `state`, sends `bank_idempotency_key`
      │   v1.7 and v1.9 pods run side by side during the rolling update;
      │   the trigger mirrors every write into the other column
      │   rollback: redeploy v1.7.3's digest; schema unchanged; still works
      ▼
 R3  (≥ 1 release and ≥ 7 days later) apply contract/0012 by hand
          preconditions: no v1.7 anywhere, and the rollback target is ≥ v1.9.0,
          no pre-1.9 `settle_build_info` series for 7 days
          rollback target after R3 is v1.9.x, which doesn't use `status`
```

In this repo R1 and R2 are delivered by the same pipeline run, but in
order. The `migrate` job runs as a Kubernetes Job and must succeed before
the `deploy` job starts. In production I'd recommend shipping R1 a day
earlier on its own, so the backfill never overlaps a rollout.

## How the pipeline enforces this

* `scripts/lint_migrations.py` runs in CI and rejects: renames, `DROP COLUMN`
  or `DROP TABLE` outside `contract/`, type changes, `SET NOT NULL`,
  `ADD COLUMN … NOT NULL`, volatile defaults, non-concurrent `CREATE INDEX`,
  missing `lock_timeout`. Run against the rejected v1.8.0 file, it
  reports 5 violations.
* The migration Job runs **before** the rollout, never in parallel, and never
  during the settlement freeze window.
* Integration tests run the migrations against real Postgres and then run
  v1.7-shaped and v1.9-shaped writes against the same schema
  (`app/tests/integration/test_migrations_pg.py`).
* The runner holds an advisory lock (one runner at a time). It retries
  `lock_timeout` failures with backoff instead of queueing behind traffic.
  It drops `INVALID` indexes left by a failed `CONCURRENTLY` build before
  retrying.

## Checklist for a new migration

1. Can the version *currently in production* run against the new schema? If
   not, split the change.
2. Does any statement rewrite the table or take `ACCESS EXCLUSIVE` for more
   than a moment? (Volatile default, type change, `SET NOT NULL`, index
   without `CONCURRENTLY`.)
3. Is `lock_timeout` set?
4. Backfills: batched, one transaction per batch, re-runnable.
5. Anything destructive goes in `contract/`, with preconditions written at
   the top.
