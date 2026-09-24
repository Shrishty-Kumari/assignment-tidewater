# SLOs and alerts for settle

Implementation: `deploy/observability/rules/settle-slo-rules.yaml`.
Runbook entries: `docs/RUNBOOK.md`.

## SLO 1: API availability and latency

| | |
|---|---|
| **SLI** | Share of requests through the ingress (`ingress="settle-api"`, i.e. `/settlements*`) that return non-5xx **and** complete in < 1 s. Source: `nginx_ingress_controller_request_duration_seconds`. |
| **Objective** | 99.5 % over a rolling 28 days |
| **Error budget** | 0.5 % ≈ 3 h 22 min of total outage per 28 days |
| **Why at nginx** | Most of the 14 Aug errors (502 connection refused, 504 timeouts, 503 no upstreams) were produced by nginx, not the app. App metrics would have undercounted the outage. |
| **Excluded** | `/reports/daily` (own ingress, 6–9 s by design; a separate latency objective belongs to the report rewrite) and health/metrics endpoints |

## SLO 2: settlement correctness and freshness

| | |
|---|---|
| **Correctness SLI** | Payout references paid more than once, according to the bank ledger (`settle_reconciliation_duplicate_payouts`). Reconciliation runs every 60 s in the worker. |
| **Correctness objective** | **0**. There is no error budget for paying a merchant twice; any occurrence is an incident. |
| **Freshness SLI** | Share of payouts recorded as paid within 15 min of being queued (`settle_payout_duration_seconds`, bucket `le="900"`) |
| **Freshness objective** | 99 % per day. The daily run normally drains in ~10 min at the bank's rate limit. |

## The five alerts

Every alert is `severity: page`, so it wakes someone up, and has a runbook
entry that says what to do. There are no "FYI" alerts. Cluster-level signals
(node disk, kubelet) belong to the platform team's generic rules.

| Alert | Fires when | Why it's actionable |
|---|---|---|
| `SettleAPIErrorBudgetBurn` | 1 h and 5 min bad ratio > 7.2 % (14.4× burn), or 6 h and 30 min > 3 % (6× burn), for 2 min | Customers are failing now; runbook starts with "was there a deploy? roll back" |
| `SettleDuplicatePayout` | any duplicate in the bank ledger | Stop the worker, reconcile, start recovery with finance |
| `SettlePayoutsDelayed` | oldest queued payout > 10 min, or payouts stuck > 15 min, or dead letters, for 5 min | Settlements will miss the bank cut-off |
| `SettleDatabaseSaturated` | connections > 85 % of `max_connections`, or Postgres down, for 2 min | Leading indicator of the 14 Aug failure mode; runbook finds the consumer |
| `SettleRestartStorm` | > 3 container restarts in 10 min in `settle` | Probes, OOM or a bad release; runbook checks the last deploy first |

## Would they have caught 14 Aug? (replayed on the evidence)

`python3 tools/replay_alerts.py` evaluates each rule minute by minute over
`incident-2026-08-14/metrics/*.csv` and the finance extract. Assumptions are
in the script header. The main one: the export's SLI is the 5xx ratio only,
so the burn-rate result is conservative.

| Alert | Rule (as replayed) | Would fire | vs detection 14:24 | vs first customer report 14:19 |
|---|---|---|---|---|
| SettleAPIErrorBudgetBurn | 5m and 1h bad ratio > 7.2 % (14.4x burn), for 2m | 14:10 | 14 min earlier | 9 min earlier |
| SettleDatabaseSaturated | connections / max_connections > 0.85, for 2m | 14:04 | 20 min earlier | 15 min earlier |
| SettleRestartStorm | increase(restarts[10m]) > 3 | 14:04 | 20 min earlier | 15 min earlier |
| SettlePayoutsDelayed | oldest queued payout > 10 min, for 5m | 14:16 | 8 min earlier | 3 min earlier |
| SettleDuplicatePayout | bank ledger shows a duplicate (first at 14:02:53), reconciliation every 60 s | 14:04 | 20 min earlier (finance found it 19.6 h later) | 15 min earlier |

What this would have changed:

* On-call would have been paged at **14:04** by three independent alerts,
  instead of hearing from support at 14:24.
* `SettleDuplicatePayout` at 14:04 would have stopped the worker two minutes
  after the second payment. The 37 duplicates could have been recalled
  the same afternoon instead of discovered the next morning.
* `SettleDatabaseSaturated` points straight at the mechanism (connections),
  which on-call only worked out at 14:46.
* The burn-rate alert fires later (14:10) because the 1 h window needs about
  5 minutes of an 80 % error rate to cross 7.2 %. That's the price of
  multi-window alerting (few false pages), and it's why a cause-based alert
  (DB saturation, restarts) sits next to it.

## Logs and correlation

* All components log one JSON object per line to stdout (`app/settle/logs.py`, ingress-nginx `log-format-upstream`).
* nginx generates `X-Request-ID` (`$req_id`). The API logs it as `request_id` and
  writes it into the queued job. The worker logs every line of that payout with
  the same `request_id` and `payout_id`, and forwards it to the bank.
* To follow one settlement end to end:

```
kubectl -n ingress-nginx logs deploy/ingress-nginx-controller | grep <request_id>
kubectl -n settle logs -l app.kubernetes.io/name=settle-api    | grep <request_id>
kubectl -n settle logs -l app.kubernetes.io/name=settle-worker | grep <request_id>
```
