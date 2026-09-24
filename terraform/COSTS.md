# Rough monthly cost (eu-west-1, on-demand list prices, 730 h/month)

Estimates use public list prices as of writing and exclude tax, support and
data transfer beyond what's listed. Treat them as ±15 %. Check them with the
AWS Pricing Calculator before committing to a budget.

## Staging / testing (target < USD 250)

| Item | Sizing | USD/month |
|---|---|---:|
| EKS control plane | 1 cluster, standard support | 73.00 |
| EC2 nodes | 2 × t4g.medium (2 vCPU / 4 GiB, Graviton) @ ~$0.0368/h, fixed at 2 (no scale-out) | 53.70 |
| Node EBS | 2 × 20 GB gp3 | 3.50 |
| RDS Postgres 15 | db.t4g.micro, single-AZ @ ~$0.018/h | 13.10 |
| RDS storage + PI | 20 GB gp3 (autoscaling off), 1-day backups, Performance Insights 7-day (free tier) | 2.50 |
| ElastiCache Redis 7 | 1 × cache.t4g.micro @ ~$0.018/h | 13.10 |
| NAT gateway | 1 × $0.048/h + ~20 GB processed | 36.00 |
| NLB for ingress-nginx | 1 NLB + ~1 LCU | 20.50 |
| CloudWatch Logs | ~5 GB/month ingested (EKS audit, flow logs, RDS), 7-day retention | 3.00 |
| Secrets Manager | 3 secrets + RDS-managed master secret | 1.60 |
| KMS | 1 CMK (+ requests) | 1.20 |
| ECR | ~2 GB images | 0.20 |
| Enhanced monitoring | 60 s, 1 instance | ~1.50 |
| **Total** | | **≈ 222** |

Headroom: about USD 28 (≈ 11 %).

**Why not smaller.** `db.t4g.micro` and `cache.t4g.micro` are already the
smallest Graviton classes. `t4g.small` nodes allow only 11 pods each with the
VPC CNI (the stack needs ~17 plus 4 system pods per node) and have 2 GB RAM.
Going smaller needs CNI prefix delegation and a raised max-pods setting, and
leaves Prometheus/Grafana short of memory. The remaining large items are
fixed costs: the EKS control plane ($73), NAT ($36) and the NLB ($20).

**Testing-phase settings** (staging only): deletion protection off (a final
snapshot is still taken on destroy), 1-day backups, storage autoscaling off,
7-day logs. Revert these before staging holds data anyone cares about.

### Trade-offs made to stay under budget

| Choice | Saves (approx.) | What we give up | Why it's acceptable for staging |
|---|---:|---|---|
| **Single NAT gateway** instead of one per AZ | $35 | An AZ outage takes staging egress (bank mock / external calls) down | Staging availability isn't customer-facing. Prod has 2. |
| **Single-AZ RDS**, no standby | $13+ | Failover becomes restore/reboot (minutes) | No customer impact. PITR still enabled (7 days). |
| **Burstable Graviton** (t4g) nodes, RDS and Redis | ~$150 vs m7g | CPU credits can run out under sustained load tests | Load tests run in a dedicated window or against a temporarily up-sized stack. |
| **1 Redis node**, no replica | $13 | Redis restart loses in-flight queue state | The worker's DB claim + idempotency key make redelivery safe, and payouts are re-enqueued from DB state (runbook). |
| **No interface VPC endpoints** (ECR API, STS, Secrets Manager, Logs) | ~$60 (4 × 2 AZ × $7.30) | That traffic goes through the NAT gateway | Low volume in staging. The free S3 gateway endpoint takes the heavy ECR layer pulls. |
| **7-day log retention** | small | Shorter history | Testing phase; prod keeps 365 days. |
| **Prometheus/Grafana in-cluster** (kube-prometheus-stack on the nodes) instead of AMP/AMG | ~$50+ | We manage it ourselves | Same stack as local, and dashboards/alerts are identical. |
| Still **ON_DEMAND** nodes | n/a | Spot could save ~$30 | Spot interruptions mid-deploy would make pipeline verification flaky. Revisit once PDBs/verification have a track record. |

Things deliberately **not** cut, even in staging: encryption with the
customer-managed key, private subnets, the private EKS endpoint, flow logs,
deletion protection and backups. Staging must behave like prod where security
and data safety are concerned.

## Production

**Currently test-sized** (testing phase): `envs/prod` uses the same sizes as
staging (≈ USD 225/month; deletion protection on, 7-day backups, 30-day logs).
The table below is the **production target** to restore before real traffic.

| Item | Sizing | USD/month |
|---|---|---:|
| EKS control plane | 1 cluster | 73 |
| Nodes | 3 × m7g.large (autoscale to 6) | ~180 |
| RDS | db.m7g.large Multi-AZ, 100 GB gp3 | ~330 |
| ElastiCache | 2 × cache.m7g.large (primary + replica, Multi-AZ) | ~230 |
| NAT | 2 gateways + data | ~80 |
| NLB, logs (365 d), Secrets Manager, KMS, ECR, backups | | ~80 |
| **Total** | | **≈ 970** |

Next savings for prod once usage is known: Savings Plans / RI for RDS and
nodes (−30 to −40 %), and interface endpoints if NAT data processing grows.
