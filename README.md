# settle


Daily merchant settlement service (settle-api + settle-worker, Postgres 15,
Redis 7, nginx ingress), hardened after the 14 Aug v1.8.0 incident. Runs
locally on k3d with the same pipeline, monitoring and policies as the AWS
design.

> The incident evidence bundle and the "inherited" starting state were
> reconstructed, because none were supplied with the brief. See the first
> commit and `incident-2026-08-14/README.md`.

## Prerequisites

Linux (tested on Ubuntu 24.04 / Debian 12, x86_64), at least 8 GB free RAM, and:

| Tool | Used for |
|---|---|
| `docker`, `k3d` ≥ 5.9, `kubectl`, `helm` | local cluster |
| `act` | running the GitHub Actions workflow locally |
| `trivy`, `cosign` ≥ 3 | image scan and signing |
| `python3.12` | tests, scripts |
| `terraform` ≥ 1.11, `tflint`, `checkov` | infrastructure checks (optional) |

Shortcut: `make prereqs` installs whichever of `docker`, `k3d`, `kubectl`,
`helm`, `cosign`, `act` and `trivy` are missing (`make up` runs it
automatically). The full manual list, including the Python and Terraform
tools, is:

```
# base packages, Python and Docker (log out and back in after usermod)
sudo apt-get update && sudo apt-get install -y curl git make unzip gnupg lsb-release wget openssl bc python3.12 python3.12-venv
curl -fsSL https://get.docker.com | sh && sudo usermod -aG docker "$USER"

# cluster tooling
curl -s https://raw.githubusercontent.com/k3d-io/k3d/main/install.sh | bash
curl -LO "https://dl.k8s.io/release/$(curl -Ls https://dl.k8s.io/release/stable.txt)/bin/linux/amd64/kubectl" \
  && sudo install -m 0755 kubectl /usr/local/bin/kubectl && rm kubectl
curl -fsSL https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash
curl -s https://raw.githubusercontent.com/nektos/act/master/install.sh | sudo bash -s -- -b /usr/local/bin

# scan and signing
curl -sfL https://raw.githubusercontent.com/aquasecurity/trivy/main/contrib/install.sh | sudo sh -s -- -b /usr/local/bin
curl -sLO https://github.com/sigstore/cosign/releases/latest/download/cosign-linux-amd64 \
  && sudo install -m 0755 cosign-linux-amd64 /usr/local/bin/cosign && rm cosign-linux-amd64

# infrastructure checks (optional)
wget -qO- https://apt.releases.hashicorp.com/gpg | sudo gpg --dearmor -o /usr/share/keyrings/hashicorp.gpg
echo "deb [signed-by=/usr/share/keyrings/hashicorp.gpg] https://apt.releases.hashicorp.com $(lsb_release -cs) main" \
  | sudo tee /etc/apt/sources.list.d/hashicorp.list && sudo apt-get update && sudo apt-get install -y terraform
curl -s https://raw.githubusercontent.com/terraform-linters/tflint/master/install_linux.sh | bash
sudo apt-get install -y pipx && pipx install checkov && pipx ensurepath

# Python environment (make test / make lint also create it on first use)
make venv
```

On other distributions, install the same tools with your package manager.

Ports used on your machine: **8088** (ingress), **5001** (local registry),
**6550** (Kubernetes API), **5432** (throwaway Postgres for tests, step 2),
**4566** (LocalStack, step 3).

## Test it step by step

Run the steps in order. Steps 1–3 need no cluster. Steps 4–10 run against the
local k3d cluster created in step 4. `make help` lists every target.

### 1. Get the code

`make deploy` and `make images` build the releases from the git tags
`v1.9.0` and `v1.9.1-rc`, so clone with tags:

```
git clone https://github.com/Shrishty-Kumari/assignment-tidewater.git && cd assignment-tidewater
git fetch --tags
git tag                          # expected: v1.9.0  v1.9.1-rc
make venv                        # Python 3.12 virtualenv with the dev requirements (rebuilt if broken)
```

### 2. Application, manifests and migrations (Tasks B, C)

```
make test                        # unit tests on SQLite; expected: all pass, Postgres tests skipped

# migrations 0007 → 0011 and v1.7/v1.8 side-by-side (N-1) on a real Postgres 15
docker run -d --name settle-pg-test -e POSTGRES_USER=settle -e POSTGRES_PASSWORD=test \
  -e POSTGRES_DB=settle -p 5432:5432 postgres:15
PG_TEST_URL=postgresql://settle:test@localhost:5432/settle make test   # expected: all pass (the test DB is wiped)
docker rm -f settle-pg-test

make lint                        # ruff, migration safety lint (rejects the v1.8.0 0008), DB connection budget at max HPA replicas
```

The migration redesign and release sequence are in [docs/MIGRATIONS.md](docs/MIGRATIONS.md).
The DB connection budget formula is in [docs/CHANGES.md](docs/CHANGES.md).

### 3. AWS infrastructure code (Task D)

Nothing is applied and no AWS credentials are needed.

```
make tf-check                    # terraform fmt -check, validate (staging, prod, bootstrap), tflint, checkov
                                 # expected: "all checks passed"
make tf-plan-localstack          # optional: terraform plan of staging and prod against LocalStack (starts it on :4566)
docker rm -f settle-localstack   # stop LocalStack afterwards
```

Review log, suppressed findings and their justification:
[docs/terraform-review.md](docs/terraform-review.md). Staging cost estimate
(under USD 250/month): [infra/terraform/COSTS.md](infra/terraform/COSTS.md).

### 4. Create the local environment

```
make up                          # k3d cluster, ingress-nginx, Prometheus/Grafana/Alertmanager, Kyverno,
                                 # Postgres (schema 0007 + v1.7 data), Redis, bank mock, cosign key pair
kubectl get nodes                # expected: 3 nodes Ready
kubectl get pods -A              # expected: everything Running or Completed
```

The first `make up` takes 5–10 minutes (image pulls). All passwords are
generated into Kubernetes Secrets and never printed.

### 5. Deploy the good release 1.9.0 (Task C)

```
make deploy VERSION=1.9.0        # GitHub Actions workflow run locally with act
make status                      # expected: settle-api and settle-worker at 1.9.0, image referenced by digest
curl -H "Host: settle.localtest.me" "http://localhost:8088/settlements?limit=5"   # expected: HTTP 200, JSON list
```

Expected: every job is green and the verification table is all PASS. The
pipeline runs: lint/test → build once (immutable tag, digest) → trivy image,
secret and manifest scan → cosign signature + SBOM attestation → migrations
(before the rollout) → deploy by digest → post-deploy verification → rollback
on failure.

Without act, the same steps run with `make release VERSION=1.9.0`.

Deploys are blocked from 13:45 to 14:45 UTC (the daily settlement run, see
[docs/RUNBOOK.md](docs/RUNBOOK.md#settlement-freeze-window)). To deploy inside
that window anyway, add `FREEZE_OVERRIDE=true`, for example
`make deploy VERSION=1.9.0 FREEZE_OVERRIDE=true`.

### 6. Deploy the bad release 1.9.1-rc and watch the automatic rollback (Task C)

```
make deploy VERSION=1.9.1-rc     # expected: rollout "succeeds", verification FAILS, automatic rollback
make status                      # expected: back on 1.9.0 with the same digest as in step 5
```

`1.9.1-rc` has a Postgres-only SQL bug: unit tests pass on SQLite and the pods
start and pass their probes, but `GET /settlements/{id}` returns 500. The
verification's per-version 5xx ratio catches it (about 40 % against a 1 %
limit), and the pipeline rolls back without manual action.

Manual rollback, if ever needed: `make rollback` (see [docs/RUNBOOK.md](docs/RUNBOOK.md)).

### 7. Runtime hardening (Task B)

```
# probes: liveness does not depend on Postgres, so a slow DB cannot cause a restart storm
kubectl -n settle get deploy settle-api -o jsonpath='{.spec.template.spec.containers[0].livenessProbe}{"\n"}{.spec.template.spec.containers[0].readinessProbe}{"\n"}'

# HPA limits, disruption budgets, default-deny network policies
kubectl -n settle get hpa,pdb,networkpolicy

# log rotation on every node, so an error loop cannot fill the disk (expected: 10Mi, 5)
kubectl get --raw /api/v1/nodes/k3d-settle-agent-0/proxy/configz | grep -oE '"containerLogMax(Size|Files)":[^,]*'

# graceful worker shutdown: follow an old worker pod's logs while the deployment restarts
POD=$(kubectl -n settle get pods -l app.kubernetes.io/name=settle-worker -o name | head -1)
kubectl -n settle logs -f "$POD" | grep -E "shutdown requested|settle-worker stopped" &
kubectl -n settle rollout restart deploy/settle-worker
kubectl -n settle rollout status deploy/settle-worker
```

Expected: the worker logs `shutdown requested, no longer fetching jobs`, then
`settle-worker stopped` with `"clean": true`. Every change and its reason is
in [docs/CHANGES.md](docs/CHANGES.md).

### 8. Observability: dashboards, structured logs, one alert firing (Task E)

Dashboards (each command blocks; run it in its own terminal):

```
make grafana-password            # Grafana admin password
make grafana                     # http://localhost:3000, user admin, dashboard "settle overview"
make prometheus                  # http://localhost:9090 → Alerts: 5 settle alerts
make alertmanager                # http://localhost:9093
```

JSON logs correlated across API and worker by `request_id`:

```
ID=$(curl -s -H "Host: settle.localtest.me" -H "Content-Type: application/json" \
  -d '{"merchant_id": 1, "settlement_date": "2026-09-24", "amount_minor": 1234}' \
  http://localhost:8088/settlements | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
curl -s -X POST -H "Host: settle.localtest.me" -H "X-Request-ID: readme-test-1" \
  "http://localhost:8088/settlements/$ID/execute"                  # expected: {"payout_id": ..., "state": "queued"}
kubectl -n settle logs deploy/settle-api    | grep readme-test-1   # API request line
kubectl -n settle logs deploy/settle-worker | grep readme-test-1   # the same request_id on the payout lines
```

Fire an alert:

```
make alert-demo                  # the bank mock pays one reference twice
make alerts-log                  # expected within ~1–2 min: SettleDuplicatePayout firing, with its runbook link
scripts/alert-demo.sh clear      # reset; the alert resolves after the next reconciliation
```

Which alerts would have fired on 14 Aug, and how much earlier than the actual
detection (replayed from the evidence bundle's metrics):

```
python3 tools/replay_alerts.py   # expected: SettleDatabaseSaturated, SettleRestartStorm and SettleDuplicatePayout at 14:04, 20 min before detection
```

SLO definitions and alert rationale: [docs/SLOs.md](docs/SLOs.md).

### 9. Optional stretch (Task F)

```
make admission-demo              # expected: signed release ADMITTED; unsigned image and tag reference REJECTED by Kyverno
make chaos-db                    # +3 s latency on every Postgres response; expected: 0 restarts, DB connections
                                 # stay flat (~10/100), requests get a 503 in ~3 s or a 504 at 10 s
make chaos-db-off                # expected: automatic recovery, HTTP 200 again
kubectl -n settle get networkpolicy   # default-deny for the namespace plus explicit allows
```

### 10. Incident analysis and pre-built images (Task A)

The RCA ([docs/RCA.md](docs/RCA.md)) cites files and lines in
[incident-2026-08-14/](incident-2026-08-14/). The capacity arithmetic can be
checked by hand against `incident-2026-08-14/metrics/`.

```
make images                      # builds settle-api 1.9.0 and 1.9.1-rc from their git tags into images/
docker load < images/settle-api-1.9.0.tar.gz
```

### Clean up

```
make down                        # delete the k3d cluster
```

### Screen-recording order

`make up` → `make deploy VERSION=1.9.0` → `make deploy VERSION=1.9.1-rc`
(automatic rollback) → `make alert-demo` + `make alerts-log`.

## Repository

```
app/                   api, worker, migration runner, bank mock, tests, Dockerfile
deploy/k8s/            base + overlays (local, aws-staging) + migration Job
deploy/nginx/          ingress-nginx values, edge nginx snippet
deploy/observability/  Prometheus/Grafana values, SLO rules + 5 alerts, dashboard
deploy/policy/         Kyverno install values + image-signature policy
migrations/            0007, 0008–0011 (expand), contract/, rejected/ (the v1.8.0 file)
infra/terraform/       AWS: modules/settle, envs/staging, envs/prod, bootstrap
scripts/               pipeline steps (ci/), checks, demos
local/                 k3d config, bootstrap, local dependencies
incident-2026-08-14/   evidence bundle
```

## Documentation

| Document | Contents |
|---|---|
| [docs/RCA.md](docs/RCA.md) | 14 Aug incident: timeline, root causes, evidence, action items |
| [docs/CHANGES.md](docs/CHANGES.md) | every defect fixed and why, including the DB connection budget formula |
| [docs/DECISIONS.md](docs/DECISIONS.md) | ADRs (EKS vs ECS, migrations, probes, rollback, secrets, payouts, signing) |
| [docs/MIGRATIONS.md](docs/MIGRATIONS.md) | 0008 redesign and release sequence |
| [docs/SLOs.md](docs/SLOs.md) | SLOs, alerts, when they would have fired on 14 Aug |
| [docs/RUNBOOK.md](docs/RUNBOOK.md) | deploy, manual rollback, one entry per alert |
| [docs/terraform-review.md](docs/terraform-review.md), [infra/terraform/COSTS.md](infra/terraform/COSTS.md) | AWS design review, checks, suppressions, cost estimate |
| [docs/NOT-DONE.md](docs/NOT-DONE.md) | what is left out, in priority order |
| [docs/AI-USAGE.md](docs/AI-USAGE.md) | AI tool usage |
