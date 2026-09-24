# settle runbook

Local cluster context: `k3d-settle`. In AWS, substitute the EKS context. Namespaces:
`settle` (app, data stores locally), `monitoring`, `ingress-nginx`.

- [Deploy](#deploy)
- [Roll back manually](#roll-back-manually)
- [Settlement freeze window](#settlement-freeze-window)
- Alerts: [SettleAPIErrorBudgetBurn](#settleapierrorbudgetburn) ·
  [SettleDuplicatePayout](#settleduplicatepayout) ·
  [SettlePayoutsDelayed](#settlepayoutsdelayed) ·
  [SettleDatabaseSaturated](#settledatabasesaturated) ·
  [SettleRestartStorm](#settlerestartstorm)
- [Deploy rejected by admission](#deploy-rejected-by-admission) · [Rotate secrets](#rotate-secrets) · [Useful commands](#useful-commands)

---

## Deploy

Normal path: the pipeline does everything, including the rollback.

```
git tag v1.9.2 <commit> && git push origin v1.9.2   # (or locally: tag only)
make deploy VERSION=1.9.2                            # runs .github/workflows/deploy.yml with act
```

What happens, in order: lint + tests (incl. Postgres integration) → image built
**once** and pushed as an immutable tag (digest recorded) → trivy scan gate →
freeze-window check → the running digest is recorded as the rollback target →
migration Job (expand-only) → rollout of api + worker by digest → post-deploy
verification (≈ 2–3 min) → on failure: automatic `rollout undo` to the
recorded digest, then the old version is verified.

Without act (same scripts): `make release VERSION=1.9.2`.

Check what's running: `make status` (the VERSION column comes from the
`settle.paylane.io/version` annotation, and IMAGE is always a digest).

## Roll back manually

Use this when the pipeline isn't running (e.g. an alert fires hours after a release).

1. **Is it the release?** Check `make status` and Grafana "Running versions" /
   "5xx ratio by version". If errors started with the new version, roll back.
2. Roll back both deployments to their previous ReplicaSet (images are
   digests, so "previous" really is the previous artifact):
   ```
   make rollback
   # = kubectl -n settle rollout undo deployment/settle-api
   #   kubectl -n settle rollout undo deployment/settle-worker  (+ rollout status)
   ```
   To go to a specific older release:
   `kubectl -n settle rollout history deployment/settle-api` →
   `kubectl -n settle rollout undo deployment/settle-api --to-revision=N`
   (same for the worker).
3. **Never** run a down-migration. Migrations are backward compatible by
   design (docs/MIGRATIONS.md). If you believe the schema is the problem,
   escalate to the settle dev on call; don't hand-edit the schema.
4. Verify the rollback: `python3 scripts/ci/verify_release.py --version <previous version> --duration 30`.
5. The worker drains in-flight payouts on SIGTERM (≤ 45 s), so rolling back
   during the settlement run is safe. Still, prefer to wait if the run is
   nearly done and the error doesn't affect payouts.
6. Post in #settle-oncall: version rolled back from/to, time, reason.

Note: `rollout undo` doesn't update the `last-applied-configuration`
annotation (kubectl warns about this). The next pipeline deploy re-applies
the full manifests, so this is harmless.

## Settlement freeze window

No deploys 13:45–14:45 UTC, because the settlement run starts at 14:00 (RCA
CF1). The pipeline enforces it. An override (`freeze_override: true`, or
`FREEZE_OVERRIDE=true` for `make release`) needs a reason in the run title and
the payments lead's OK.

---

## SettleAPIErrorBudgetBurn

**Meaning.** More than 7.2 % of API requests through nginx are 5xx or slower
than 1 s (1 h and 5 min windows), or more than 3 % sustained over 6 h. At that
rate the 28-day budget is gone in about 2 days.

1. **Recent deploy?** `make status`, Grafana → "5xx ratio by version". If the
   new version is the one failing → [roll back](#roll-back-manually). Don't debug first.
2. **Which status codes?** Grafana → "Ingress requests by status".
   * `503` from the app with `Retry-After` → the database is failing. See the DB panels and
     [SettleDatabaseSaturated](#settledatabasesaturated). `/healthz/deps`:
     `kubectl -n settle exec deploy/settle-api -- python -c "import httpx;print(httpx.get('http://localhost:8000/healthz/deps').text)"`
   * `502/504` from nginx → pods restarting or overloaded: `kubectl -n settle get pods`,
     [SettleRestartStorm](#settlerestartstorm).
   * `500` → application errors: `kubectl -n settle logs -l app.kubernetes.io/name=settle-api --since=10m | grep '"level": "ERROR"'`.
3. **Slow, not failing?** Check p99 by route. If only `/reports/daily`, it's on
   its own ingress and excluded from the SLO, so something else is slow.
4. **Capacity?** The HPA max is 6 by design (connection budget). Don't raise
   it without re-running `make lint` (budget check).
5. Resolve: the alert clears once the 5-minute window is healthy. Record the
   budget spent in the incident doc.

## SettleDuplicatePayout

**Meaning.** The bank ledger shows a payout reference paid more than once.
Money has left twice. **Severity: incident. Page the payments lead.**

1. **Stop the bleeding.** Pause payout execution:
   `kubectl -n settle scale deploy/settle-worker --replicas=0`
   Queued jobs stay in Redis and nothing is lost. The API keeps accepting settlements.
2. **Which references?** Worker logs:
   `kubectl -n settle logs -l app.kubernetes.io/name=settle-worker --since=2h | grep "duplicate payouts"`.
   Locally the bank mock lists them:
   `kubectl -n settle exec deploy/settle-worker -- python -c "import httpx;print(httpx.get('http://bankmock.settle.svc:8080/v1/ledger/duplicates').json())"`.
3. For each reference, compare with settle's DB (one row per payout):
   `SELECT id, state, bank_ref, bank_idempotency_key FROM payouts WHERE id = <n>;`
   If `bank_idempotency_key` is NULL, the payment bypassed the worker's claim (old version or a manual call).
4. Find the cause before restarting workers: a recent deploy of an old
   version? Someone calling the bank manually? Idempotency keys expiring at the bank?
5. Restart workers (`--replicas=2`), then hand the list to finance for recall.
   Write an incident doc.

## SettlePayoutsDelayed

**Meaning.** The oldest queued payout is older than 10 min, or payouts have
been in `queued/sending` for more than 15 min, or payouts are dead-lettered.

1. Are workers running and consuming? `kubectl -n settle get pods -l app.kubernetes.io/name=settle-worker`;
   Grafana "Payout jobs by outcome", "Queue depth" (jobs / retry / dead).
2. **Retrying?** `settle_jobs_total{outcome="retried"}` is rising, so the bank or the
   DB is failing. Worker logs: `grep "will retry"`. The bank returning 5xx/429 means
   contacting the bank. The DB means [SettleDatabaseSaturated](#settledatabasesaturated).
3. **Dead letters** (`queue="dead"` > 0): each needs a decision.
   ```
   kubectl -n settle exec deploy/redis -- redis-cli LRANGE settle:dead 0 -1
   ```
   * Bank rejected the payout (4xx): fix the data with the payments team, then re-queue
     with `redis-cli RPOPLPUSH settle:dead settle:jobs`.
   * Payout stuck in `sending`: check the bank for its `bank_idempotency_key` first.
     Re-queuing is safe (same key → the bank replays) as long as the bank still
     holds the key.
4. **Workers idle but the queue is growing?** Check the worker heartbeat keys
   (`redis-cli KEYS 'settle:worker:*'`). A dead worker's jobs are
   recovered automatically after 30 s.

## SettleDatabaseSaturated

**Meaning.** More than 85 % of Postgres `max_connections` are in use (or
Postgres is down). This is how 14 Aug became a 48-minute outage.

1. Who holds the connections?
   ```
   kubectl -n settle exec postgres-0 -- psql -U postgres -d settle -c \
     "select usename, application_name, state, wait_event_type, count(*) from pg_stat_activity group by 1,2,3,4 order by 5 desc"
   ```
   (`application_name` = pod name.) In AWS: Performance Insights.
2. **Lots of `Lock` waits?** Find the blocker:
   `select pid, now()-xact_start as age, query from pg_stat_activity where pid in (select unnest(pg_blocking_pids(pid)) from pg_stat_activity);`
   A migration or manual session holding a lock: cancel it (`select pg_cancel_backend(<pid>)`) and tell its owner.
3. **settle_app near its role limit (88)?** More pods than the budget
   allows? `kubectl -n settle get hpa,deploy`. The HPA max and pool sizes are
   budgeted (`make lint`). Someone may have changed them by hand.
4. **Idle connections from dead pods** (only possible if TCP keepalive failed):
   `select pg_terminate_backend(pid) from pg_stat_activity where usename='settle_app' and state='idle' and state_change < now()-interval '10 min';`
5. Pods don't restart because of DB saturation (probes don't use the DB), so
   don't restart them to "fix" it. That adds connection churn.

## SettleRestartStorm

**Meaning.** More than 3 container restarts in `settle` in 10 minutes.

1. Why? `kubectl -n settle get pods` and
   `kubectl -n settle describe pod <pod> | sed -n '/Last State/,/Events/p'`
   * `OOMKilled` → memory limit. Check the last release and raise the limit via a PR.
   * `Error` + liveness failures → the process is hung. Probes don't touch the DB, so
     this is a real app problem. Get a thread dump/logs.
   * `Evicted` → node pressure: `kubectl describe node <node>`. That's the platform team.
2. Did it start with a deploy? → [roll back](#roll-back-manually).
3. Crash-looping pods don't take the DB down (bounded pools, backoff), but
   they do reduce capacity. Check the SLO burn.

## Deploy rejected by admission

`kubectl apply` or a rollout fails with
`settle-verify-image-signature: … no signatures found` or `missing digest`.

* **Expected** for anything that didn't go through the pipeline: a manually
  built image, a tag instead of a digest, a hotfix pushed from a laptop.
  **Don't** disable the policy. Build and release through the pipeline (`make deploy`).
* If a pipeline-built image is rejected: check that the scan job's
  "Sign image + attest SBOM" step ran, and that the policy's public key
  matches the pipeline key: `cosign verify --key local/.secrets/cosign.pub
  --insecure-ignore-tlog=true localhost:5001/settle-api@<digest>`.
* If Kyverno itself is down, **every** settle-api pod creation is blocked
  (failurePolicy Fail, deliberately). Running pods keep running. Restore
  Kyverno (`kubectl -n kyverno get pods`) instead of relaxing the policy.
* Rotate the signing key: generate a new pair, add its public key as a second
  attestor entry, re-sign current releases, then remove the old key.

---

## Rotate secrets

Locally: `kubectl -n settle delete secret postgres-admin settle-db settle-db-migrate`
and `make down && make up` (throwaway environment).

AWS:
1. Bump `secret_string_wo_version` for the secret in `modules/settle/secrets.tf`,
   then plan and apply (a new ephemeral password is written to Secrets Manager).
2. Run the db-bootstrap Job, which sets the role's password from the new secret.
3. ESO refreshes the k8s Secret within `refreshInterval` (1 h). Force it with
   `kubectl -n settle annotate externalsecret settle-db force-sync=$(date +%s) --overwrite`.
4. `kubectl -n settle rollout restart deploy/settle-api deploy/settle-worker`.

**Leaked in CI on 14 Aug:** the production DB URL (base64, run #412) and the
kubeconfig. Rotate both, purge the run logs (Actions → run → delete logs),
and check the Postgres logs for connections from unknown hosts since 14 Aug.

## Useful commands

```
make status                      # versions, digests, pods
make grafana / prometheus / alertmanager
make alerts-log                  # alerts as delivered
make chaos-db / chaos-db-off     # 3 s DB latency
kubectl -n settle logs -l app.kubernetes.io/name=settle-worker -f | jq -c 'select(.level!="INFO")'
```
