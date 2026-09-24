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

```
# base packages, Python and Docker (log out and back in after usermod)
sudo apt-get update && sudo apt-get install -y curl git make unzip gnupg lsb-release wget python3.12 python3.12-venv
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

# Python environment
python3.12 -m venv .venv && .venv/bin/pip install -r app/requirements-dev.txt
```

On other distributions, install the same tools with your package manager.

Ports used on your machine: **8088** (ingress), **5001** (local registry), **6550** (Kubernetes API).

## Run it

```
make up                          # cluster, ingress, Prometheus/Grafana, Kyverno, Postgres, Redis, bank mock
make deploy VERSION=1.9.0        # release 1.9.0 through the pipeline
make status                      # what is running (version + image digest)
```

The first `make up` takes 5–10 minutes (image pulls). After that the API is at:

```
curl -H "Host: settle.localtest.me" http://localhost:8088/settlements?limit=5
```

Dashboards: `make grafana` (http://localhost:3000, user `admin`, password
from `make grafana-password`), `make prometheus`, `make alertmanager`.

Tear down: `make down`.

## Test it

### Code and configuration

```
make test                        # unit tests (SQLite)
PG_TEST_URL=postgresql://user:pass@localhost:5432/db make test   # + migration/N-1 tests on real Postgres (DB is wiped)
make lint                        # ruff, migration safety lint, DB connection budget
make tf-check                    # terraform fmt/validate, tflint, checkov
make tf-plan-localstack          # terraform plan of staging + prod against LocalStack
```

### Delivery pipeline: good release, then automatic rollback

```
make deploy VERSION=1.9.0        # expected: all jobs green, verification table all PASS
make deploy VERSION=1.9.1-rc     # expected: rollout "succeeds", verification FAILS, automatic rollback to 1.9.0
```

`make deploy` runs `.github/workflows/deploy.yml` with act: lint/test → build
once (immutable tag, digest) → trivy scan → cosign sign + SBOM attestation →
migrations (before the rollout) → deploy by digest → post-deploy verification →
rollback on failure.

`1.9.1-rc` (branch `release/1.9.1-rc`) has a Postgres-only SQL bug: unit tests
pass on SQLite, the pods start and pass their probes, but `GET
/settlements/{id}` returns 500. The verification's per-version 5xx ratio
catches it (≈ 40 % against a 1 % limit), and the pipeline rolls back.

Without act, the same steps run with `make release VERSION=...`.

### Alerts, admission policy, chaos

| Command | Expected |
|---|---|
| `make alert-demo`, then `make alerts-log` | `SettleDuplicatePayout` fires within about 1 minute, with its runbook link. Reset: `scripts/alert-demo.sh clear` |
| `make admission-demo` | signed release admitted; unsigned image and tag reference rejected by Kyverno |
| `make chaos-db`, then `make chaos-db-off` | 3 s DB latency: 0 restarts, DB connections stay flat (~10/100), requests get a 503 in ~3 s or a 504 at 10 s, automatic recovery |

### Pre-built images

```
make images                                       # builds settle-api 1.9.0 and 1.9.1-rc from their git tags
docker load < images/settle-api-1.9.0.tar.gz
```

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
