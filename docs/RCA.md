# RCA: settle v1.8.0 deploy, 14 Aug 2026

| | |
|---|---|
| **Status** | Final (blameless) |
| **Impact window** | 14:03 – 14:51 UTC (48 min) of elevated API errors; double payouts at 14:02:53 – 14:03:02 |
| **Customer impact** | Peak 81 % of API requests failed (502/504/500/503). 37 merchants were paid twice (EUR total in `finance/reconciliation-note-2026-08-15.txt`). |
| **Detection** | Customers → support at 14:19 (17 min after errors began). Double payments found by finance the next morning at 09:40. |
| **Evidence** | `incident-2026-08-14/` (paths below are relative to it). All times are UTC. |

> Evidence provenance: this bundle was reconstructed for the assignment (see
> `incident-2026-08-14/README.md`). Citations are `file:line`. For CSVs, line
> 2 is 13:30, so minute *HH:MM* is on line `2 + minutes since 13:30`.

---

## 1. Summary

Release v1.8.0 shipped migration `0008`. In one transaction, that migration
renamed `payouts.status` to `state`, added a `NOT NULL` column with a
*volatile* default (`gen_random_uuid()`), and built an index without
`CONCURRENTLY`. The volatile default forced Postgres to rewrite the 11 GB
`payouts` table while holding an `ACCESS EXCLUSIVE` lock, for **7 min 43 s**.
The pipeline ran this migration in a job with no ordering relative to the
deploy. As a result, the rollout of API and worker pods happened while the
table was locked.

Three things then made it worse:

1. **Restart storm.** Every request that touched `payouts` blocked and held
   a DB connection. Postgres ran out of connection slots. The liveness probe
   needs a DB connection and fails after a single 1 s timeout, so every API
   pod was restarted at once. The HPA read the restart CPU as load and scaled
   to 10 replicas. At 10 replicas, even the *idle* connection pools (200)
   exceed Postgres's 97 usable slots. So the outage kept going for 41 minutes
   **after** the lock was released.
2. **Double payouts.** Rolling the workers killed pods whose jobs had already
   been paid at the bank but were blocked on the `UPDATE`. The first new pod
   re-queued every job in the shared processing list, and the new pods paid
   those 37 payouts again. The bank call carries no idempotency key.
3. **DiskPressure.** The restarting pods retried the DB connection in a tight
   loop without sleeping. Each attempt logged a traceback to a new, unrotated
   `hostPath` log file. That filled node-2's disk and triggered evictions.

The manual rollback took 13 minutes. `kubectl rollout undo` re-pulled the
same `:latest` digest, and v1.7.3 needed a hand-run down-migration, which
itself broke the v1.8.0 pods that were still running.

---

## 2. Timeline (UTC, 14 Aug 2026)

| Time | Event | Evidence |
|---|---|---|
| 13:12:44 | Single corrected ECC error on node-2 *(not causal, §6)* | `node/dmesg-node-2.txt:2-3` |
| 13:30 → | `/reports/daily` returns 504 once a minute, all afternoon *(pre-existing, §6)* | `nginx/access.log:18,134` |
| 13:40–13:52 | pg_dump backup; node-3 CPU 92 % *(not causal, §6)* | `postgres/postgresql.log:2-3`, `metrics/node_cpu.csv:12-24` |
| 13:58:10 | HPA `FailedGetResourceMetric` (metrics-server blip), recovered 13:59:40 *(not causal)* | `kubectl/events.txt:3-4` |
| 14:00:00–14:00:41 | Daily settlement run: scheduler executes 2,412 settlements; payouts are queued | `nginx/access.log` (POST `/execute` burst from 10.0.4.20), `app/settle-worker.log:6` |
| 14:00:31 | Pipeline run #412 starts, triggered by tag `v1.8.0` | `ci/deploy-run-412.log:2` |
| 14:01:41 | 4 tests pass. They run on SQLite with a hand-written post-0008 schema; `pytest \|\| true` would have passed anyway | `ci/deploy-run-412.log:8-9` |
| 14:01:58 | `migrate` job starts on a second runner. It has no `needs:`, so it runs in parallel with `build`. The DB URL is printed in base64 | `ci/deploy-run-412.log:12-15` |
| **14:02:05.415** | **0008 begins: `RENAME COLUMN status TO state` takes `ACCESS EXCLUSIVE` on `payouts`, then `ADD COLUMN ... DEFAULT gen_random_uuid()` starts a full rewrite** | `postgres/postgresql.log:13-15` |
| 14:02:05–14:02:15 | v1.7.3 workers call the bank (`bank_payout_ok`), then block on `UPDATE payouts SET status='paid'` | `app/settle-worker.log:75-114`, `postgres/postgresql.log:19+` (`still waiting for RowExclusiveLock`) |
| 14:02:31 | Image built; `:latest` and `:v1.8.0` both point to `sha256:8f3c…` | `ci/deploy-run-412.log:20-21` |
| 14:02:35 | Trivy finds 1 CRITICAL and 13 HIGH; ignored (`--exit-code 0`) *(not causal)* | `ci/deploy-run-412.log:23-24` |
| 14:02:37 | Pipeline `cat`s the kubeconfig into the job log | `ci/deploy-run-412.log:29` |
| **14:02:40** | `kubectl apply -f deploy/k8s/` changes both pod templates (new `hostPath` log volume). Both Deployments start rolling. `:latest` with `imagePullPolicy: Always` pulls 1.8.0 | `ci/deploy-run-412.log:32-35`, `kubectl/events.txt:5-15` |
| 14:02:44 | First v1.7.3 worker killed (exit 143, no shutdown handling), with 10 paid-but-unrecorded jobs in flight | `app/settle-worker.log:115` |
| 14:02:50 | First API `health check failed` (`QueuePool limit of size 5 overflow 10 reached`) | `app/settle-api.log:14-20` |
| **14:02:52** | **First v1.8.0 worker starts and "recovered 40 orphaned jobs from settle:processing"** | `app/settle-worker.log:116-117` |
| **14:02:53–14:03:02** | **v1.8.0 workers pay the same 37 payouts a second time** (e.g. payout 881542 at `:75` and `:118`) | `app/settle-worker.log:118-158`, `finance/duplicate-payouts-2026-08-15.csv` |
| 14:03:02 | Postgres starts refusing connections (`remaining connection slots are reserved…`, `too many clients`) | `postgres/postgresql.log:136-137`, `metrics/pg_connections.csv:35` (97/100) |
| 14:03:04 | Liveness failures begin across all API pods, old and new. Each is followed by `Killing` | `kubectl/events.txt:21,27` |
| 14:03:10 | Restarted pods spin in `wait_for_db()`: about 800 tracebacks per 10 s per process | `app/settle-api.log:67,91` |
| 14:03 | 5xx ratio 12 %, then 38 % (14:04), 61 % (14:05), 81 % (14:08) | `metrics/api_requests.csv:35-40` |
| 14:04:40 / 14:05:41 | HPA scales `settle-api` 4 → 8 → 10 on "cpu above target" | `kubectl/events.txt:40,52-53`, `kubectl/get-hpa-watch.txt` |
| 14:05:10 | Scheduler retries of `POST /execute` are retried again by nginx (`non_idempotent`); the second attempt gets 409 *(not causal, §6)* | `nginx/access.log:4352` (+13 more) |
| **14:09:48.161** | **0008 commits (rewrite 441 s + index 21 s). The 37 orphaned v1.7.3 `UPDATE ... status` sessions fail with `column "status" does not exist`, then `Broken pipe`** | `postgres/postgresql.log:377-396`, `:397+` |
| 14:09:48 | v1.8.0 workers' blocked `UPDATE ... state='paid'` succeed. The 37 payouts are recorded as paid **once** | `app/settle-worker.log:164-200` |
| 14:10 → 14:47 | Connections drop to 88, then sit at the limit (97) for 37 min. 5xx ratio stays at 34–41 % | `metrics/pg_connections.csv:42-78`, `metrics/api_requests.csv:43-62` |
| 14:19 | First customer reports reach support | `notes/oncall-channel-export.txt:3` |
| 14:19:31 | Settlement queue drained (7.5 min late) | `app/settle-worker.log:201`, `metrics/queue_depth.csv:51` |
| 14:26 | On-call acknowledges | `notes/oncall-channel-export.txt:5` |
| **14:31:05** | **node-2 DiskPressure.** Five API pods and one worker evicted. Rescheduling blocked by taint and CPU | `kubectl/events.txt:143-153`, `app/settle-api.log:1924` (ENOSPC) |
| 14:31–14:38 | 5xx ratio rises to 55–68 % | `metrics/api_requests.csv:63-69` |
| 14:38:20 | `kubectl rollout undo` for API and worker **re-pulls digest `8f3c…` (1.8.0)** because both revisions say `:latest` | `kubectl/events.txt:170-173`, `kubectl/rollout-history.txt` |
| 14:41:07 | Pipeline run cancelled | `ci/deploy-run-412.log:55` |
| 14:43:10 | v1.7.3 re-pushed as `:latest` from a laptop | `ci/deploy-run-412.log:56-57` |
| 14:44:02 | Manual down-migration (1.8 s). The running v1.8.0 pods now fail with `column p.state does not exist`; 5xx reaches 86 % | `postgres/postgresql.log:922-924`, `app/settle-api.log:2668+`, `metrics/api_requests.csv:76` |
| 14:45:31 | `rollout restart` pulls `2b7e…` (1.7.3) | `kubectl/events.txt:200-201` |
| 14:46:50 | HPA `maxReplicas` patched to 4 | `kubectl/events.txt:204` |
| 14:47:15 | Idle `settle` backends terminated | `postgres/postgresql.log:982` |
| 14:51 | Error rate back to baseline | `metrics/api_requests.csv:83` |
| 14:58 | `/var/log/settle/settle.log` deleted by hand on node-2; DiskPressure clears | `notes/oncall-channel-export.txt:18` |
| 15 Aug 09:40 | Finance: 37 payouts paid twice | `finance/reconciliation-note-2026-08-15.txt` |

---

## 3. Root causes

### RC1: A non-backward-compatible, table-locking migration ran concurrently with the rollout

* `migrations/0008_payout_state.sql` does three dangerous things in **one
  transaction**:
  1. `RENAME COLUMN status TO state` breaks every v1.7.x query the moment it
     commits. The running version and the new version cannot share this
     schema, so a rolling update is guaranteed to fail somewhere.
  2. `ADD COLUMN ... NOT NULL DEFAULT gen_random_uuid()`. Postgres 11+ can
     only add a column without a rewrite when the default is *non-volatile*.
     `gen_random_uuid()` is volatile, so all 38.6 M rows were rewritten under
     `ACCESS EXCLUSIVE` (`postgresql.log:377`, 441 s; `:396`, 11 GB).
  3. `CREATE INDEX` without `CONCURRENTLY`, inside the same lock (21 s,
     `:393`).
  * There is no `lock_timeout`, so the migration waited as long as it needed.
    Everything else then queued behind it (`still waiting for …Lock`,
    `:16` onward).
* The pipeline (`.github/workflows/deploy.yml`, as used on the day):
  * The `migrate` job has **no `needs:`**. It started at 14:01:58 on a free
    runner while `build` was still running (`ci/deploy-run-412.log:14`).
  * It re-runs *every* `*.sql` file with `|| true`.
  * No step checks migration safety.
  * The migration and the pod rollout overlapped by construction.
* `kubectl apply -f deploy/k8s/` started the rollout at 14:02:40, not
  `set image`. v1.8.0 changed both pod templates (new log volume). With
  `image: …:latest` and `imagePullPolicy: Always`, that pulled whatever
  `:latest` pointed at. The "deploy API first, workers after `rollout status`"
  ordering in the script therefore never applied (`events.txt:5-7`).

### RC2: Health checks, connection pools and the HPA turned a slow dependency into a self-sustaining outage

**Liveness depended on Postgres.** `/healthz` checks out a pooled connection,
runs `SELECT 1` and pings Redis (`app/settle/api.py`). The probe has
`timeoutSeconds: 1` and `failureThreshold: 1`, and readiness uses the same
endpoint (`kubectl/describe-pod-…xk2lp.txt`, "Liveness … timeout=1s …
#failure=1").

When the pool was exhausted, `QueuePool … timeout 30.00`
(`settle-api.log:20`), or Postgres refused connections (`:67`), every pod
failed its probe at the same moment and kubelet restarted every pod
(`events.txt:21` onward). A dependency shared by *all* pods must never be
able to fail *liveness*.

**Killed pods exited with code 137.** `terminationGracePeriodSeconds: 5` is
shorter than gunicorn's 30 s graceful timeout, so kubelet SIGKILLed the pods
(`describe-pod`, "Exit Code: 137"). The backends of killed clients that were
waiting on the lock kept their connection slots until the lock was released.
Postgres doesn't notice a dead client while a backend is waiting on a lock
(`client_connection_check_interval = 0`). At 14:09:48 those sessions
surfaced as `Broken pipe` (`postgresql.log:397` onward).

**Capacity arithmetic.** This shows why the outage outlived the lock.

| Quantity | Value | Source |
|---|---|---|
| Postgres slots usable by `settle` | 100 − 3 (`superuser_reserved_connections`) = **97** | `postgresql.log:1` |
| gunicorn workers per API pod | 4 | `app/Dockerfile` (`-w 4`) |
| Max connections per API process | `DB_POOL_SIZE` + `DB_MAX_OVERFLOW` = 5 + 10 = **15** | `deploy/k8s/configmap.yaml` |
| Idle connections kept per API process | `DB_POOL_SIZE` = **5** | SQLAlchemy `QueuePool` semantics |
| Worker pods × threads | 4 × 10 (each thread holds 1 conn; pool cap 15 > 10) | configmap, `worker.py` |

* **Before the deploy (4 API pods):** worst case is 4 × 4 × 15 + 4 × 10 =
  240 + 40 = **280**, already 2.9× over 97. In practice it was 69 during
  the batch (`pg_connections.csv:32`), because pools are lazy and traffic is
  light. The system had been running on luck.
* **During the rollout** (`maxSurge: 100%`, `maxUnavailable: 50%`, so 2 old +
  4 new API pods, plus 1 surge worker): 6 × 4 × 15 + 5 × 10 = **410**.
* **At HPA max (10) with idle pools only:** 10 × 4 × 5 = **200** connections
  held idle. That is more than 97 before a single request is served.
* **At HPA max with overflow:** 10 × 4 × 15 + 40 = **640**, 6.6× over.

So once the HPA reached 10 (14:05:41), the service **could not recover by
itself** even after the lock was released at 14:09:48. Connections went back
to 97 within a minute (`pg_connections.csv:42-46`). Recovery only came once
someone manually capped the HPA at 4 (`events.txt:204`, 14:46:50) and
terminated idle backends (`postgresql.log:982`, 14:47:15):
4 × 4 × 5 = 80 idle ≤ 97.

**The HPA amplified the outage.** CPU requests were 100 m. A process spinning
in `wait_for_db()` (no sleep) uses a full core, so utilisation read as
180–420 % of target (`get-hpa-watch.txt`). Each extra replica added up to 60
more connections of demand and another spinning process. That is positive
feedback.

### RC3: Payout execution was neither idempotent nor safe to shut down

How the 37 double payouts happened (`app/settle/worker.py`):

1. `handle()` calls the bank **first**, with no idempotency key (`bank.py`).
   It then runs `UPDATE payouts`, and only after that removes the job from
   `settle:processing`. So between "money moved" and "fact recorded" there
   is a window.
2. At 14:02:05 that window stretched to 7 minutes. Every v1.7.3 worker thread
   that got `bank_payout_ok` then blocked on `UPDATE payouts SET status='paid'`
   (`settle-worker.log:75-114`; `postgresql.log:19+`, STATEMENT
   `UPDATE payouts SET status…`).
3. The rolling update killed each old worker 14–48 s after its jobs had been
   paid. The worker has no SIGTERM handling, so it exited with code 143
   immediately and abandoned its threads (`settle-worker.log:115,138,160,162`).
4. `recover_orphans()` in the first new pod moved **all 40** entries of the
   *shared* `settle:processing` list back to `settle:jobs`
   (`settle-worker.log:117`). There is no per-worker ownership or lease. It
   cannot tell "worker died" from "worker is busy", or "not yet paid" from
   "paid, not yet recorded".
5. The v1.8.0 pods re-executed those jobs. 37 of them had already been paid,
   so the bank paid them again (`settle-worker.log:118-158`). 3 had been
   killed *before* the bank call and were correctly paid once.
6. Only the v1.8.0 `UPDATE ... state` could succeed after the commit
   (`settle-worker.log:164-200`). The v1.7.3 sessions failed on the renamed
   column (`postgresql.log:397` onward).
   * So settle's own DB shows exactly one paid payout per settlement.
   * The duplicates exist only at the bank.
   * Finance's integrity query confirms this: no duplicate payout rows
     (`finance/reconciliation-note-2026-08-15.txt`).
7. **Why exactly twice and not more?**
   * `recover_orphans()` is guarded by a 10-minute `SET NX` lock, so only one
     pod re-queued anything.
   * The v1.8.0 worker pods have no liveness probe, so nothing killed them
     while they were blocked.
   * Both are luck, not design.

The duplicates are the same `payout_id`s: the first payment at 14:02:05–14:02:15
and the second at 14:02:53–14:03:02
(`finance/duplicate-payouts-2026-08-15.csv`). That matches the worker rollout
window to the second, and rules out the ingress retries (§6).

A latent second path exists in the current code. `process()` re-queues a job
on **any** exception, even one raised after the bank call succeeded,
immediately and with no attempt limit. Any DB error after payment therefore
causes unbounded repeat payments.

---

## 4. Contributing factors

| # | Factor | Evidence |
|---|---|---|
| CF1 | **Deployed during the daily settlement window** (batch at 14:00). A worker rollout during a payout run is the highest-risk moment for in-flight jobs. | `settle-worker.log:6`, `ci/deploy-run-412.log:2` |
| CF2 | **Mutable image tag.** `:latest` + `Always` meant `apply` rolled pods, and `rollout undo` restored the *same digest*, which lost ~7 min | `kubectl/rollout-history.txt`, `events.txt:171,173` |
| CF3 | **Rollback depended on a down-migration** that was itself incompatible with the version still running | `postgresql.log:922-924`, `settle-api.log:2668` |
| CF4 | **Logs written to an unrotated `hostPath` at DEBUG**, fed by a retry loop with no backoff. Rate: node-2 free space fell from 20.1 GiB (14:03) to 10.1 GiB (14:30), i.e. **0.37 GiB/min ≈ 6.3 MiB/s**. Cross-check: node-2 hosted 6 API pods × 4 processes = 24 spinning processes × ~80 tracebacks/s (`settle-api.log:91`, "828 times in 10s") × ~3.3 KB ≈ 6.2 MiB/s. Eviction threshold `nodefs.available < 10%` = 9.8 GiB, so (20.1 − 9.8) / 0.37 ≈ **28 min, i.e. 14:31**, matching `events.txt:143`. Kubelet rotates `/var/log/pods` (1.3 G) but not hostPaths (`node/node-2-du.txt`). The file outlived the pods and kept node-2 tainted until 14:58. | `node/node-2-df.txt`, `metrics/node_disk_free.csv:35-63` |
| CF5 | **No topology spread.** 6 of 12 API pods landed on node-2, concentrating the log volume | `kubectl/get-pods-1412.txt` |
| CF6 | **The pipeline cannot fail.** `pytest \|\| true`, `rollout status … \|\| true`, Trivy `--exit-code 0`, migrations `\|\| true`. There is no post-deploy verification and no automated rollback | `.github/workflows/deploy.yml` |
| CF7 | **Tests never exercise Postgres or the migrations.** The SQLite fixture schema is hand-written | `ci/deploy-run-412.log:9`, `app/tests/conftest.py` |
| CF8 | **No alerting.** Customers detected the outage 17 min after errors began, and finance detected the double payments ~19.5 h after they happened | `notes/oncall-channel-export.txt:3` |
| CF9 | **Logs without request IDs, in plain text.** You cannot follow one request from nginx to the API to the worker | `app/settle-api.log:1` |

---

## 5. Security findings surfaced during the investigation

These did not cause the outage. They are real, and some need action now.

| Finding | Evidence | Action |
|---|---|---|
| DB URL including password printed (base64) in CI log | `ci/deploy-run-412.log:15` | **Treat the credential as compromised: rotate now.** Purge the run log. |
| Kubeconfig `cat`'d into CI log | `ci/deploy-run-412.log:29` | Rotate the cluster credential and SA token |
| DB password in `app/Dockerfile` `ENV` (so in every image layer) and in `deploy/k8s/secret.yaml` in git | repo | Remove, rotate, and scrub the registry |
| Worker container `privileged: true` | `deploy/k8s/worker-deployment.yaml` | Remove |
| Full `python:3.12` base image, root user, 1 CRITICAL / 13 HIGH CVEs | `ci/deploy-run-412.log:23-24` | Use a slim image, run as non-root, gate on scan results |
| Terraform draft: static AWS keys in provider, public RDS, `*:*` IAM, public S3 | `infra/terraform/main.tf` | Rewrite (`docs/terraform-review.md`) |

---

## 6. Alarming but not causal

| Observation | Why it's not causal |
|---|---|
| `/reports/daily` returns **504** every minute (`nginx/access.log:18,134`, `error.log`) | Present from 13:30, long before the deploy. The report takes 6–9 s and `proxy-read-timeout` is 5 s. A real defect (finance cannot load the report), fixed in this repo, but unrelated to the incident. |
| nginx **re-sent `POST /settlements/{id}/execute`** to a second upstream (`proxy_next_upstream … non_idempotent`, `nginx/access.log:4352`, 14 cases) | This is the first thing I suspected for the double payouts. It isn't the cause: the second attempt returned **409**, because the conditional `UPDATE settlements … WHERE status='pending'` serialises executes, and finance found **no duplicate payout rows**. The duplicates are the *same* payout references paid twice at the bank, which happens downstream of the API. Still a latent hazard, fixed. |
| node-3 CPU 92 % at 13:40–13:52 (`metrics/node_cpu.csv:12-24`) | pg_dump backup (`postgresql.log:2-3`). Ended 10 minutes before the deploy; Postgres was healthy at 14:00. |
| HPA `FailedGetResourceMetric` at 13:58 (`events.txt:3`) | metrics-server blip, recovered at 13:59:40 (`:4`) before the deploy. It did not affect the later scale-up. |
| `checkpoints are occurring too frequently` (`postgresql.log:4,6` onward) | A *consequence* of write volume: the batch at 14:00, then the 11 GB table rewrite. Worth tuning `max_wal_size`, but a symptom. |
| EDAC corrected memory error on node-2 at 13:12 (`dmesg:2-3`) | Single corrected error, count 1 of threshold 1000, 50 min earlier. Hardware ticket, not causal. |
| `Possible SYN flooding on port 8000` at 14:05:18 (`dmesg:4`) | Occurs mid restart storm: clients and nginx retries hitting pods whose listen backlog was full or closing. A symptom, not an attack. |
| `EXT4-fs warning … Directory index full` at 14:31:02 (`dmesg:5`) | Coincides with the disk filling (container snapshot churn from dozens of restarts). A symptom of CF4. |
| Trivy CRITICAL CVE-2025-6020 (`ci/deploy-run-412.log:23`) | Security debt in the fat base image. Not exploited and unrelated to the outage. Fixed by the slim base image and scan gate. |
| TLS certificate expires in 21 days (`nginx/error.log:2`) | Not causal. Action item AI-16. |
| Redis `overcommit_memory` warning (`redis/redis.log:1`) | Logged at boot on 3 Aug. Redis served normally throughout (BGSAVE fine at 14:00:41). |
| node-2 at 79 % disk *before* the deploy (`node/node-2-df.txt`, 13:30) | Image cache. It reduced the headroom (20 GiB), but the fill came from the hostPath log (CF4). Without the log loop, node-2 would have been fine. |

---

## 7. What went well

* Once the connection arithmetic was understood, capping the HPA and
  terminating idle backends restored service within 4 minutes.
* The conditional `UPDATE … WHERE status = 'pending'` in `/execute` stopped
  the nginx retries from creating duplicate payouts.
* Finance's daily bank reconciliation caught the double payments within a day.

---

## 8. Action items

Priorities: **P0** before the next deploy of settle; **P1** within 2 weeks;
**P2** this quarter. *Where* points to the fix in this repo.

| # | Action | Owner | Pri | Where |
|---|---|---|---|---|
| AI-1 | Rotate the DB credential leaked in CI run #412 and the kubeconfig; purge the run log | Platform on-call + Security | P0 | runbook §Secrets |
| AI-2 | Recover the 37 duplicate payouts with the bank | Finance Ops + Payments lead | P0 | n/a |
| AI-3 | Redesign 0008 as expand → backfill → contract; `lock_timeout` on every migration; migration lint in CI; migrations run as an ordered pipeline step, never in parallel with a rollout | DevOps + settle dev | P0 | `migrations/`, `docs/MIGRATIONS.md`, `.github/workflows/deploy.yml` |
| AI-4 | Split probes: `/livez` has no dependencies; `/readyz` is local; DB health only as a metric/alert. Add a startupProbe. Relax thresholds | DevOps | P0 | `deploy/k8s/`, `app/settle/api.py` |
| AI-5 | Connection budget formula enforced in CI; pool sizes and HPA max derived from it; `pool_timeout` 2 s | DevOps | P0 | `scripts/check_db_budget.py`, `docs/CHANGES.md` |
| AI-6 | Worker: graceful SIGTERM drain; per-worker processing lists with heartbeat leases; recover only expired leases | settle dev + DevOps | P0 | `app/settle/worker.py` |
| AI-7 | Idempotency key per payout sent to the bank; atomic claim of the payout before the bank call; bounded retries with backoff; dead-letter queue | settle dev (review: Payments lead) | P0 | `app/settle/worker.py`, `bank.py` |
| AI-8 | Immutable, digest-pinned images; build once, promote the same digest; ban `:latest` | DevOps | P0 | pipeline, manifests |
| AI-9 | Post-deploy verification with automatic rollback | DevOps | P0 | `scripts/verify-release.sh`, workflow |
| AI-10 | Logs to stdout only, as JSON; remove `hostPath`; `emptyDir.sizeLimit`; backoff on every retry loop | DevOps | P0 | manifests, `app/settle/logs.py` |
| AI-11 | SLOs and alerts for availability, DB saturation, restart storms, payout freshness and duplicate payouts | DevOps + SRE | P1 | `deploy/observability/` |
| AI-12 | Change freeze during the settlement window (13:45–14:45 UTC), enforced by the pipeline | Eng manager | P1 | workflow `freeze` step |
| AI-13 | HPA behaviour: scale-up stabilisation; CPU requests reflect reality | DevOps | P1 | `deploy/k8s/hpa.yaml` |
| AI-14 | Integration tests against real Postgres, including an N-1 compatibility test (old app on the new schema) | settle dev | P1 | `app/tests/` + CI service container |
| AI-15 | PgBouncer (transaction pooling) in AWS to decouple replicas from `max_connections` | DevOps | P2 | `docs/NOT-DONE.md` |
| AI-16 | Renew the settle TLS cert; alert on expiry < 14 days | Platform | P1 | n/a |
| AI-17 | Nightly *automated* reconciliation (bank ledger vs payouts), not only finance's manual one | settle dev + Finance | P1 | `settle_reconciliation_duplicates` metric |
