# Architecture decision records

Short ADRs: context, options, decision, consequences. Status is "accepted"
unless noted.

---

## ADR-001: Run settle on EKS rather than ECS on Fargate

**Context.** Leadership wants settle on AWS this quarter. It runs today on a
self-managed Kubernetes cluster. Everything we operate settle with is
Kubernetes-native: manifests, HPA and PDB, probes, NetworkPolicies, the
Prometheus operator stack, the pipeline (kustomize, rollout status/undo,
migration Job) and the runbooks. Staging must cost under USD 250/month.
The team is small and has just inherited the service.

**Options.**

| | EKS (managed node group) | ECS on Fargate |
|---|---|---|
| Fixed cost | $73/month control plane per cluster | none |
| Staging estimate | ≈ $223/month (COSTS.md) | ≈ $130/month |
| Reuse of what we have | Manifests, NetworkPolicies, probes, Prometheus rules, pipeline and runbooks carry over almost unchanged | Rewrite as task definitions/services. Probes become ALB/container health checks. NetworkPolicy becomes security groups per task. Prometheus needs AMP/ADOT sidecars. Pipeline becomes ECS deployments / CodeDeploy. |
| Operational load | Node patching (managed node groups + AL2023), add-ons, version upgrades every ~12 months | Least: no nodes, no upgrades |
| Safe delivery | Our verification + rollback, and Argo Rollouts later | CodeDeploy blue/green with alarm-based rollback built in |
| Portability / other services | Paylane's other services run on k8s too, so a shared EKS platform amortises the control plane | ECS-only knowledge |

**Decision.** EKS, with Graviton managed nodes, a private API endpoint,
Pod Identity for workload IAM, and VPC CNI with NetworkPolicy enforcement.

**Why.** The migration risk this quarter comes from changing *how settle is
operated*. The fixes we just made to probes, graceful shutdown, the
connection budget and network policy are expressed in Kubernetes terms.
Re-expressing them for ECS in the same quarter doubles the change surface
right after an incident. ECS is cheaper, but EKS fits the staging budget
with ~11 % headroom.

**Consequences.**
* We own node and add-on upgrades (runbook item; managed node groups roll nodes with PDBs respected).
* $73/month per cluster is fixed. Staging and prod are separate clusters (blast radius). Ephemeral review environments would be namespaces, not clusters.
* Revisit if settle stays the only workload on the platform after 2 quarters. Then ECS/Fargate's lower operational load wins, and the manifests have become the only asset to port.

---

## ADR-002: Expand/contract migrations and no down-migrations

**Context.** Migration 0008 renamed a column and rewrote a table under an
exclusive lock while old and new versions were both running (RCA RC1). The
rollback needed a hand-run down-migration, which broke the version still
running.

**Options.** (a) Keep up/down migrations and run them in a maintenance window.
(b) Blue/green databases. (c) Expand/contract: every migration is backward
compatible with the running version; destructive steps are delayed until
nothing needs the old shape.

**Decision.** (c). Forward-only runner (`settle.migrate`), a migration Job run
before the rollout and never in parallel with it, a CI linter
(`scripts/lint_migrations.py`) that rejects renames, drops, type changes,
`SET NOT NULL`, volatile defaults, non-concurrent indexes and missing
`lock_timeout`, and destructive steps parked in `migrations/contract/` with
written preconditions.

**Consequences.** Rolling back is always "redeploy the previous digest". Schema
changes take 2–3 releases (expand, migrate, contract) and need a sync trigger
or dual writes in between. The linter will sometimes need a `lint:allow` with
a reason. That's intended friction.

---

## ADR-003: Shallow probes; release health is judged by the pipeline

**Context.** `/healthz` checked Postgres and Redis and was used for liveness
and readiness with a 1 s timeout and failure threshold 1. A slow database
made every pod fail at once and kubelet restarted all of them (RC2).

**Options.** (a) Deep health checks with longer timeouts. (b) Readiness deep,
liveness shallow. (c) Both shallow; dependencies monitored and alerted on.

**Decision.** (c). `/livez` has no dependencies. `/readyz` reports local state
(draining) only. DB health is exposed as metrics and `/healthz/deps` for humans.
DB errors return a fast 503 with `Retry-After`.

**Why not (b).** Postgres is shared by all pods, so a DB-aware readiness probe
marks all pods unready together and nginx returns 503 for everything,
including requests that don't need the DB. It turns a partial failure into
a total one. It also can't tell "this pod is bad" from "the DB is bad".

**Consequences.** Probes no longer catch a release whose code is broken but
which starts up (the 1.9.1-rc case). That's the job of post-deploy
verification (ADR-004): synthetic journeys and version-scoped error rates.
The demo shows exactly this: the RC's rollout "succeeds" and verification
rolls it back.

---

## ADR-004: Pipeline-driven verification and rollback, run locally with act

**Context.** The brief asks for automated detection and rollback, demonstrated
against a local cluster. The deploy of 14 Aug had `rollout status || true`
and no verification at all.

**Options.**

| Option | Pros | Cons |
|---|---|---|
| Argo Rollouts canary + AnalysisTemplates (Prometheus) | Progressive traffic shifting, analysis during the rollout, industry standard | Another controller + CRDs to learn during the handover; needs traffic splitting via nginx annotations; more moving parts to demo |
| Pipeline step: deploy → verify (synthetic + Prometheus) → `rollout undo` | Simple, visible in CI logs, the same scripts run under act, on GitHub or via make | Whole-release rollout (not canary); the pipeline must stay alive to roll back |
| Rely on probes/`progressDeadlineSeconds` only | Nothing to build | Misses every bug that doesn't crash the pod (the RC) |

**Decision.** Pipeline verification now (`scripts/ci/verify_release.py` +
`rollback.sh`). Gates: HTTP 5xx ≤ 1 %, transport errors ≤ 5 %, an end-to-end
payout paid, Prometheus 5xx ratio and p95 latency *for the new version's
label*, zero restarts, zero duplicate payouts. It fails closed on missing
data. Argo Rollouts canary is the next step (NOT-DONE #2), and the same
Prometheus queries become its AnalysisTemplate.

**Where it runs.** GitHub Actions workflow, executed locally with **act**
(`make deploy VERSION=…`). A GitHub-hosted runner can't reach a laptop
cluster. On a real GitHub repo, the lint, test, build and scan jobs run on
hosted runners and `deploy` needs a self-hosted runner in the cluster's
network (in AWS: a runner inside the VPC, assuming the OIDC deployer role).

**Consequences.** Verification adds ~3 minutes per deploy. If the pipeline
itself dies mid-release, nothing rolls back automatically, but the
`SettleAPIErrorBudgetBurn` alert still pages. Tags are immutable and the
build step promotes an existing version instead of rebuilding it.

---

## ADR-005: Secrets via Secrets Manager + External Secrets Operator + Pod Identity

**Context.** The DB password was in the Dockerfile, a committed Secret
manifest, the Terraform draft and (base64) in the CI log.

**Options.** (a) Sealed Secrets in git. (b) Secrets Store CSI driver
(mounted files). (c) External Secrets Operator syncing Secrets Manager into k8s
Secrets. (d) IAM database authentication (no password at all).

**Decision.** (c) now: Secrets Manager (customer KMS key) holds the values,
written by Terraform with write-only arguments so they're not in state.
ESO, authenticated by EKS Pod Identity with read access to exactly three
secret ARNs, creates `settle-db`, `settle-db-migrate` and `settle-redis`.
settle-api and settle-worker have no AWS permissions at all. The RDS master
secret is managed and rotated by RDS. Locally, `bootstrap.sh` generates
random secrets straight into the cluster.

**Consequences.** Values end up as Kubernetes Secrets (base64, encrypted at rest
by EKS KMS envelope encryption, readable by whoever can read Secrets in the
namespace, so RBAC matters). Rotation is manual (bump the version + db-bootstrap
Job) until (d) is implemented (NOT-DONE #4), which removes the app DB password
entirely.

---

## ADR-006: Effectively-once payouts: DB claim + bank idempotency key

**Context.** Redis queue delivery is at-least-once, and on 14 Aug redelivery
paid 37 merchants twice (RC3). Exactly-once *delivery* isn't achievable
across Redis, Postgres and the bank. Exactly-once *effect* is.

**Options.** (a) Transactional outbox + a separate dispatcher reading
Postgres (no Redis in the payment path). (b) Keep the Redis queue, and make
execution idempotent with a DB claim and a bank idempotency key. (c) Rely on
the bank de-duplicating by our `reference` field (it doesn't: the mock, like
the real API, pays every accepted request).

(a) is the cleaner long-term design but changes the business flow and
the deployment topology during a stabilisation quarter. (c) isn't available.

**Decision.** Before calling the bank, the worker claims the payout in
Postgres (`queued|sending → sending`) and gets a stable
`bank_idempotency_key`, which it sends as `Idempotency-Key`. A redelivered
job either finds the payout `paid` (acknowledged without calling the bank) or
replays the same key (the bank returns the original payout). Per-worker
processing lists with heartbeat leases limit redelivery to dead workers;
bounded retries with backoff go to a dead-letter list.

**Consequences.** The bank must honour idempotency keys, and the key's
retention at the bank must exceed our maximum retry horizon (8 attempts,
≤ ~15 min). **To confirm with the bank before go-live.** A payout stuck in
`sending` after dead-lettering needs a human to reconcile against the bank
(runbook). This touches the payment path, so it needs review by the payments
lead even though the business rules are unchanged.

---

## ADR-007: Signed images, verified at admission

**Context.** On 14 Aug, `:latest` meant nobody could say which image was
running, and a laptop could push anything to the registry (it did, at
14:43). Digest pinning answers "which image". It doesn't answer "did this
come from the pipeline, tested and scanned?"

**Options.** (a) Registry permissions only. (b) cosign signing + an admission
controller (Kyverno or Sigstore policy-controller). (c) Signing plus SBOM/provenance
attestations with policy on their content.

**Decision.** (b) now, with the SBOM attached as a signed attestation (the
first step of c). The pipeline signs a digest only after the vulnerability
and secret scans pass. Kyverno (`failurePolicy: Fail`) admits settle-api
images in `settle` only if they're pinned by digest and signed by the
pipeline key. Locally that's a cosign key pair from `make up`. In AWS it's a KMS
asymmetric key (Terraform) or keyless OIDC signing.

**Consequences.** Hotfixes must go through the pipeline, even under pressure.
That's the point. If Kyverno is down, new settle-api pods can't be created.
Running pods are unaffected, but it adds a dependency to the incident
checklist. Kyverno 1.19 deprecates `ClusterPolicy` in favour of
`ImageValidatingPolicy`, so the migration is on NOT-DONE.
