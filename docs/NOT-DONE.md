# Not done, and what I'd do with two more weeks (in priority order)

1. **Confirm the bank's idempotency guarantees.** ADR-006 assumes the partner
   bank honours `Idempotency-Key` and keeps keys longer than our retry horizon
   (~15 min). This is a question for the bank, not code, but the whole
   double-payment fix depends on it. Until it's confirmed, `SettleDuplicatePayout`
   and the reconciliation are the safety net.
2. **Argo Rollouts canary.** Today a bad release reaches 100 % of pods for
   ~3 minutes before verification rolls it back. A canary (10 % → 50 % → 100 %)
   with the same Prometheus queries as an AnalysisTemplate limits the blast
   radius to 10 % and makes the rollback independent of the CI job staying alive.
3. **PgBouncer** (transaction pooling) in front of RDS. It decouples replica
   count from `max_connections`. The HPA max of 6 is set by the connection
   budget, not by CPU.
4. **IAM database authentication** for settle_app / settle_owner. It removes
   the application DB passwords entirely, and with them the manual rotation
   and the Secrets Manager rotation suppression (CKV2_AWS_57).
5. **Keyless signing in AWS + CEL policies.** Local signing uses a cosign key
   pair (Kyverno verifies it at admission, done). In AWS, sign keyless with the
   GitHub OIDC identity or with the KMS key from `infra/terraform`, upload to Rekor,
   and migrate the Kyverno `ClusterPolicy` (deprecated in 1.19) to
   `ImageValidatingPolicy`. Also verify the SBOM attestation at admission, not
   just the signature.
6. **Durable, automated reconciliation.** Today the worker compares with the bank
   mock's ledger endpoint every minute. In production this should be a daily
   job against the bank statement file (plus the intraday API if the bank has
   one), writing results to a table finance can query.
7. **Load test + real capacity numbers.** The HPA target (70 % CPU), the 6-replica
   max and the worker concurrency are reasoned, not measured. A k6 test at
   2–3× the daily run would replace the reasoning with data.
8. **Report endpoint.** `/reports/daily` takes 6–9 s because it's computed on
   every request. A materialised view refreshed after the settlement run would
   make it ~100 ms and allow a normal timeout (it currently has its own 20 s ingress).
9. **Platform-level alerts and log shipping.** Node disk, kubelet and cert
   expiry belong to the platform team's generic rules (deliberately not in
   settle's 5 alerts). Logs should go to Loki/CloudWatch with retention, not
   just `kubectl logs`.
10. **Redis durability in AWS.** Staging runs a single node. A Redis loss is
    recovered by re-enqueuing payouts still `queued/sending` from Postgres.
    That procedure should be a script with a test, not a runbook paragraph.
11. **Apply-level test of the Terraform.** A LocalStack *plan* is done
    (`make tf-plan-localstack`). An apply needs EKS/RDS/ElastiCache emulation
    (LocalStack Pro) or a sandbox AWS account with a budget alarm.
12. **Contract step for 0008** (`migrations/contract/0012`): scheduled for one
    release after 1.9.0 has been stable in production, per its preconditions.
13. **API tests against Postgres, not only SQLite.** The unit tests use SQLite,
    so dialect bugs (like the 1.9.1-rc `GROUP BY`) are only caught after
    deploy. Running the API suite on the CI Postgres service would move that
    detection to before the build.
