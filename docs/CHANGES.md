# Changes: every defect fixed, and why

Each entry links the defect to the incident (RCA reference) or marks it as
a security or latent problem. Commit messages have more detail. The **why**
matters more than the what.

## Connection budget (the formula)

Postgres accepts `max_connections − superuser_reserved_connections` client
connections. settle must fit inside that at the **worst moment it can
legitimately reach**: HPA at max, during a rollout, with old pods still
terminating.

```
demand = Σ over deployments d:
           pods_max(d) × processes(d) × (DB_POOL_SIZE(d) + DB_MAX_OVERFLOW(d))
         + reserved                      (migrate Job 2 + postgres-exporter 1)

pods_max(d) = maxReplicas(HPA) or replicas
            + maxSurge                   (extra pod during a rolling update)
            + 1                          (a terminating pod keeps its pool until it exits)

budget = max_connections − superuser_reserved_connections − admin_headroom
       = 100 − 3 − 5 = 92
```

| deployment | max | surge | pods | processes | pool + overflow | connections |
|---|---|---|---|---|---|---|
| settle-api | 6 (HPA) | 1 | 8 | 4 (gunicorn) | 2 + 0 | 64 |
| settle-worker | 2 | 1 | 4 | 1 | 5 + 0 (4 payout threads + housekeeping) | 20 |
| reserved | | | | | | 3 |
| **demand** | | | | | | **87 ≤ 92** |

The same formula on the 14 Aug manifests: 10 + 10 surge + 1 = 21 pods × 4 ×
(5 + 10) + 60 = **1,323** against 92. Even the steady state at HPA max
(idle pools only, 10 × 4 × 5 = 200) exceeded Postgres. That's why the
outage didn't end when the migration lock was released (RCA RC2).

Enforcement:
* `scripts/check_db_budget.py` reads every input (HPA max, replicas, maxSurge,
  `GUNICORN_WORKERS`, pool env) from the **rendered manifests**. It runs in CI and
  again as a release gate in `deploy.sh`. Raising the HPA max or a pool size
  without re-doing the arithmetic fails the pipeline.
* Defence in depth: the `settle_app` DB role has `CONNECTION LIMIT 88`, so the
  app can never take the admin headroom. RDS pins `max_connections = 100` so
  the budget doesn't silently change with instance size.
* `pool_timeout` 2 s (was 30 s): when the pool is exhausted, the request fails
  fast with 503 + `Retry-After` instead of holding a thread and a client
  connection.
* Scaling beyond this needs PgBouncer (transaction pooling) (NOT-DONE #3), not
  bigger pools.

## app/Dockerfile

| Defect | Why it mattered | Fix |
|---|---|---|
| `ENV DATABASE_URL=…password…` | Production password in every image layer and registry copy (RCA §5) | Removed. Secrets only at runtime from a Secret. Rotate the credential. |
| Shell-form `CMD` | PID 1 is `/bin/sh`; SIGTERM delivery to gunicorn depends on the shell | Exec form: gunicorn is PID 1 |
| `python:3.12` full image, apt tools (curl, vim, netcat, psql), root user | ~1 GB, 1 CRITICAL / 13 HIGH CVEs on 14 Aug, bigger attack surface | `python:3.12-slim-bookworm` pinned by digest, multi-stage venv, `apt upgrade`, no tools, uid 10001, 80 MB |
| `COPY . .` with no `.dockerignore` | Tests, `.git` and local files in the image; cache busted on every change | Repo-root context with an allow-list `.dockerignore` |
| Unpinned `requirements.txt` | Non-reproducible builds (CI resolved newer fastapi on the day) | All pins exact |
| `HEALTHCHECK` curl | Ignored by Kubernetes; needed curl in the image | Removed; probes live in the manifests |
| No version inside the image | Metrics and logs couldn't say which release served a request | `SETTLE_VERSION` from the build arg; every metric and log line carries it |

## deploy/k8s

| Defect | Incident link | Fix |
|---|---|---|
| Liveness **and** readiness on `/healthz`, which checks Postgres + Redis, `timeoutSeconds: 1`, `failureThreshold: 1` | RC2: slow DB → every pod restarted at once | `/livez` (no deps) for startup/liveness (3 × 10 s), `/readyz` (local only) for readiness; DB health via metrics and alerts |
| `terminationGracePeriodSeconds: 5`, no preStop | RC2/RC3: SIGKILL mid-request (exit 137) and mid-payout (exit 143) | api 45 s + preStop 10 s + gunicorn graceful 25 s; worker 60 s > 45 s drain |
| Worker `command: sh -c …` | Signals depend on the shell | Exec form |
| `maxSurge: 100%`, `maxUnavailable: 50%` | Doubled connection demand and halved capacity at once | 1 / 0; `progressDeadlineSeconds: 180` so a stuck rollout fails fast |
| `image: …:latest` + `imagePullPolicy: Always` | CF2: `apply` rolled pods uncontrolled; `rollout undo` re-pulled the bad image | Placeholder image, replaced by a digest in the pipeline; `IfNotPresent` |
| Pools 5 + 10 per process, `pool_timeout` 30, HPA 4–10 | RC2: 1,323 potential connections vs 97 | Budgeted pools (2 + 0 api, 5 + 0 worker), HPA 2–6, enforced (above) |
| HPA scale-up unbounded, CPU request 100 m | Crash-loop CPU read as load: 4 → 10 in 61 s | Scale-up +2 pods/min after 60 s stabilisation, slow scale-down, realistic requests (250 m) |
| `hostPath: /var/log/settle` + `LOG_FILE`, `LOG_LEVEL: DEBUG` | CF4: 10.5 GB unrotated file filled node-2, DiskPressure, evictions | stdout only (kubelet rotates 10 Mi × 5), `emptyDir` with `sizeLimit` for `/tmp`, INFO level |
| No topology spread | CF5: 6 of 12 API pods on node-2 | Spread by hostname and zone |
| No PodDisruptionBudget | Node drains could take out all replicas | PDB `maxUnavailable: 1` |
| `secret.yaml` with the production password in git | Security (RCA §5) | Deleted. Secrets created by bootstrap locally, External Secrets in AWS |
| Worker `privileged: true` "for debugging" | Container = root on the node | Removed. All pods: non-root, read-only rootfs, no privilege escalation, drop ALL, seccomp; namespace enforces PSS `restricted` |
| Default service account token mounted | Any compromised pod could call the k8s API | Dedicated SAs, `automountServiceAccountToken: false` |
| `NodePort 30080` | Bypassed nginx (and its timeouts/limits) | ClusterIP only |
| Ingress routed `/` (incl. `/metrics`) + `configuration-snippet` | Internal endpoints public; snippets = arbitrary nginx config | Explicit public paths only; snippets disabled |
| No NetworkPolicy | Any pod could reach Postgres, Redis, the bank | Default deny + explicit flows (Task F) |
| Migrations ran from CI in parallel with the rollout | RC1 | Migration Job with its own DDL credential, run before and never during the rollout |

## deploy/nginx (ingress-nginx values + edge snippet)

| Defect | Why | Fix |
|---|---|---|
| `proxy_next_upstream … http_50x non_idempotent`, 5 tries | Re-sent `POST /execute` to other pods on 14 Aug (409 only thanks to a conditional UPDATE) | `error` only, 2 tries. Never re-send POSTs, never on read timeout (chaos test: timeout retries doubled DB load) |
| `proxy-read-timeout: 5` globally | `/reports/daily` (6–9 s) returned 504 every time, all day (pre-existing) | 10 s API, 20 s for `/reports` on its own ingress |
| `upstream-keepalive-connections: 0`, HTTP/1.0 upstream | New TCP connection per request | Keepalive 64, HTTP/1.1; gunicorn keepalive 75 s > nginx 60 s |
| Plain-text log format without request id | CF9: no way to follow a request | JSON log with `request_id`; `X-Request-ID` generated and forwarded |
| `server-tokens`, snippets, underscores in headers, `error-log-level: debug`, body 100 m | Hardening, and debug logs are another disk filler | All off / warn / 1 m |
| No controller metrics | The SLI must include nginx-generated 5xx | Metrics + ServiceMonitor on |

## Application (operability only; no business-rule changes)

| Defect | Incident link | Fix |
|---|---|---|
| `wait_for_db()` tight loop (no sleep, full traceback per attempt) at startup | CF4 (disk), RC2 (CPU → HPA) | Removed from API startup. Worker backs off 0.5 → 30 s, one line per attempt |
| Probes depend on the DB | RC2 | `/livez`, `/readyz`, `/healthz/deps` split |
| DB errors → 500 after a 30 s pool wait | Slow failure, ties up threads | 503 + `Retry-After` after ≤ 2 s; statement/lock/idle-in-tx timeouts per session; TCP keepalives |
| Worker: bank call without idempotency key, shared processing list, recovery of *all* in-flight jobs on every start, no SIGTERM handling, immediate infinite re-queue on error | RC3: 37 double payouts | DB claim + `Idempotency-Key`, per-worker lists + heartbeat leases, SIGTERM drain, bounded backoff + dead-letter queue (ADR-006) |
| Text logs to a file | CF4, CF9 | JSON to stdout with `request_id` / `payout_id` |
| No metrics | CF8: nobody noticed for 17 min | Prometheus metrics for the SLOs and alerts |
| Migration runner didn't exist (psql loop with `\|\| true`) | RC1 | `settle.migrate`: forward-only, advisory lock, `lock_timeout` retries, no-transaction support, connect retry |

## Migrations

`0008` replaced by expand → backfill → validate → (contract later). See
`docs/MIGRATIONS.md`. Linted in CI.

## Pipeline (.github/workflows/deploy.yml)

| Defect | Fix |
|---|---|
| `pytest \|\| true`, tests only on SQLite | Tests gate; integration tests on Postgres 15 incl. N-1 compatibility; ruff with security rules; migration lint |
| `migrate` job without `needs:`, `psql … \|\| true` for every file | Migration Job inside `deploy`, after build and scan, before the rollout; failure stops the release |
| Image rebuilt and tagged `:latest` | Built once per version, immutable tag, deployed by digest; an existing version is promoted, not rebuilt |
| Trivy `--exit-code 0` + `continue-on-error` | Fixable HIGH/CRITICAL fail the build; secret scan; SBOM; k8s misconfig scan |
| `echo $(… DATABASE_URL | base64)`, `cat ~/.kube/config` | No DB credentials in CI at all; kubeconfig from a secret into a 0600 file, never printed |
| `rollout status … \|\| true`, no verification, no rollback | Post-deploy verification (synthetic + Prometheus, fails closed), automatic rollback to the recorded digest, re-verification |
| `actions/checkout@master`, default token permissions | Major-version pins, `permissions: contents: read`, `concurrency` (one release at a time) |
| Deployed during the settlement run | Freeze window 13:45–14:45 UTC enforced |
| Nothing proved an image came from the pipeline | cosign signs every scanned digest and attaches the SBOM as an attestation. Kyverno (failurePolicy Fail) admits only signed, digest-pinned settle-api images. |

## infra/terraform

Rewritten. The 17 findings and their impact are in `docs/terraform-review.md`.

## Found while testing the fixes (not in the original repo)

| Found by | Problem | Fix |
|---|---|---|
| First deploy on k3d | Migration Job's ServiceAccount/NetworkPolicies were only created by the rollout | Prerequisites applied before the Job; fail fast if the Job can't create a pod |
| First deploy on k3d | Migration pod started before k3s had programmed its NetworkPolicy allow rule (connection refused) | Runner retries the initial connection with backoff |
| Verification run | Prometheus image has no `wget`; verification treated "no data" as zero and passed | Query via the API service proxy; every gate fails closed |
| Verification run | Zero-error releases produced "no data" for the 5xx ratio (series doesn't exist) | `(… or vector(0))` on the numerator only |
| act run | ~0.5 % transport errors from `host.docker.internal` counted as release errors | Transport errors gated separately (≤ 5 %) from HTTP 5xx (≤ 1 %) |
| `make chaos-db` | Read-timeout retries doubled requests to a slow DB (20 s responses) | `proxy-next-upstream: error` |
| Clean-room demo run | The first 1.9.1-rc regression (a missing column) was rejected by unit tests, so the RC never reached deploy/rollback | RC regression changed to a Postgres-only dialect bug (SQLite accepts it); API tests on Postgres added to NOT-DONE |
| Clean-room demo run | Prometheus 5xx window was a fixed tail that over-weighted the verifier's last polling (reported 100 % instead of ~40 %) | Window spans the whole verification period |
| Watching a pipeline run | `make deploy` passed the kubeconfig to act on the command line (visible in `ps`) | 0600 secret file for `--secret-file`, deleted afterwards |
| Signed release runs | Two local runs failed on GitHub download hiccups (cosign, trivy installer), unrelated to the release | Tool downloads retry with timeouts; `make deploy` uses a runner image with the tools baked in and a persistent trivy DB cache |
