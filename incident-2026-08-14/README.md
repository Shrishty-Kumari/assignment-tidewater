# Incident evidence bundle - 14 Aug 2026 (settle v1.8.0)

> **Provenance.** No evidence bundle was provided with the assignment brief.
> This bundle was reconstructed from the brief's description by
> `tools/reconstruct_evidence.py` (deterministic, seed 20260814). It is
> internally consistent by construction; treat it as the "given" evidence for
> the RCA in `docs/RCA.md`.

All times are **UTC**. Sampling rates are stated at the top of each log.

| Path | What it is |
|------|------------|
| `ci/deploy-run-412.log` | GitHub Actions log of the v1.8.0 deploy (jobs interleaved) |
| `nginx/access.log`, `nginx/error.log` | ingress-nginx logs (access sampled 1:20) |
| `app/settle-api.log`, `app/settle-worker.log` | aggregated pod logs |
| `postgres/postgresql.log` | Postgres 15 log (`log_lock_waits=on`, `log_min_duration_statement=1s`) |
| `redis/redis.log` | Redis 7 log |
| `kubectl/*` | events, describe pod/node, HPA watch, rollout history, pod list |
| `node/*` | node-2 `df`, `du`, `dmesg` |
| `metrics/*.csv` | Grafana CSV exports, 1-minute resolution, 13:30-15:00 |
| `finance/*` | reconciliation note and list of duplicate payouts (15 Aug) |
| `notes/oncall-channel-export.txt` | on-call chat export |
