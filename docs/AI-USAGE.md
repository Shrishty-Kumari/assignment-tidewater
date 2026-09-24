# AI usage

**Extent:** substantial. The reconstructed baseline and evidence bundle,
application changes, manifests, pipeline, scripts and first
drafts of all documents were generated in that session, and then run and
tested on a real local k3d cluster.

## Where it was used

| Area | How |
|---|---|
| Missing inputs | No starter repo or evidence bundle was supplied (HR confirmed the candidate should build everything). The AI reconstructed the "inherited" repository with its defects, and wrote `tools/reconstruct_evidence.py` to generate a consistent evidence bundle from one scripted timeline. This is disclosed in the README, the first commit and the RCA header. |
| RCA | Drafted from the generated evidence, with `file:line` citations checked against the files by script. |
| Code, manifests,| Generated, then executed: unit + Postgres integration tests, `make up`, real deploys, the RC rollback, chaos test, alert demo, `terraform validate`, tflint, checkov, trivy. |
| Docs | ADRs, runbook, SLOs, CHANGES, cost estimate: drafted by the AI from what was built and measured. |

## What had to be corrected (found by running things, not by reading)

* **The migration Job couldn't start on the first real deploy.** Its
  ServiceAccount and NetworkPolicies were only created by the rollout step that
  runs *after* migrations. The pipeline now applies prerequisites first, and
  fails fast if the Job can't create a pod.
* **NetworkPolicy race:** the migration pod started before k3s had programmed
  its allow rule (connection refused). Fixed with a connect retry in the
  runner rather than a sleep.
* **Verification that passed without evidence.** The Prometheus image has no
  `wget`, so every metric query failed. The restart and duplicate-payout gates
  treated "no data" as 0 and *passed*. Rewritten to query through the
  Kubernetes API proxy, with every gate failing closed. A second bug: a
  release with zero errors has no 5xx series, so the ratio was "no data".
* **Metrics labelled with the code version, not the release version**, so
  version-scoped verification found nothing. The image now carries the
  release version from the build.
* **Retry amplification**, found by the chaos test: nginx retried read
  timeouts on another pod, doubling load on the slow DB (20 s responses).
* **The demo RC didn't reach the rollback.** Its first regression was
  caught by the unit tests, so nothing was deployed. It was replaced by a
  Postgres-only bug that SQLite-based tests miss (a real gap, now on
  NOT-DONE).
* **A misleading number:** the verifier's Prometheus window over-weighted its
  own final polling and reported a 100 % error ratio for the RC (really ~40 %).
* **Credential in argv:** `make deploy` passed the kubeconfig to act as a
  command-line argument, visible via `ps`. Now a 0600 secret file.
* **Flaky tool downloads** broke two signed release runs (a 22-minute hang
  on a TLS drop). Fixed with retries/timeouts and a local runner image
  with the CI tools baked in.
* **Environment assumptions:** port 8080 was taken by another project's
  container (ingress moved to 8088), k3d named the registry differently than
  assumed, the CI Postgres service name doesn't resolve under act's host
  networking, the semver regex rejected `1.9.1-rc-test1`, and act on Apple
  silicon needed an explicit architecture.

