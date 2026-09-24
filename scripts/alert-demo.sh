#!/usr/bin/env bash
set -euo pipefail
NS=settle

if [ "${1:-}" = "clear" ]; then
  kubectl -n $NS rollout restart deploy/bankmock >/dev/null
  kubectl -n $NS rollout status deploy/bankmock --timeout=120s >/dev/null
  echo "bank mock ledger reset; the alert resolves after the next reconciliation (~1-2 min)"
  exit 0
fi

kubectl -n $NS exec deploy/settle-worker -- python -c '
import httpx
for _ in range(2):
    r = httpx.post("http://bankmock.settle.svc:8080/v1/payouts",
                   json={"merchant_id": 1, "amount_minor": 4200, "currency": "EUR", "reference": "payout-demo-duplicate"})
    print("bank:", r.status_code, r.json()["bank_ref"])
'
cat <<EOF

Paid reference payout-demo-duplicate twice (no Idempotency-Key).
Now watch, in order:
  1. metric   settle_reconciliation_duplicate_payouts -> 1   (<= 60 s, worker reconciliation)
  2. alert    SettleDuplicatePayout pending -> firing          (Prometheus: make prometheus -> Alerts)
  3. delivery make alerts-log                                  (Alertmanager -> alert-sink, includes runbook link)
Reset with: scripts/alert-demo.sh clear
EOF
