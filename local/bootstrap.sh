```bash
#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

CLUSTER=settle
REGISTRY_HOST=localhost:5001
INGRESS_NGINX_CHART=4.15.1
KPS_CHART=91.5.0
KYVERNO_CHART=3.9.1
PG_EXPORTER_CHART=8.2.0

log() {
  printf '\033[1;34m==> %s\033[0m\n' "$*"
}

rand() {
  openssl rand -hex 16
}

for bin in k3d kubectl helm docker openssl cosign; do
  command -v "$bin" >/dev/null || {
    echo "missing: $bin" >&2
    exit 1
  }
done

log "cluster"

if ! k3d cluster list "$CLUSTER" >/dev/null 2>&1; then
  k3d cluster create --config local/k3d.yaml --wait
fi

kubectl config use-context "k3d-$CLUSTER" >/dev/null

kubectl wait \
  --for=condition=Ready \
  nodes \
  --all \
  --timeout=180s >/dev/null

log "helm repositories"

helm repo add ingress-nginx \
  https://kubernetes.github.io/ingress-nginx >/dev/null 2>&1 || true

helm repo add prometheus-community \
  https://prometheus-community.github.io/helm-charts >/dev/null 2>&1 || true

helm repo add kyverno \
  https://kyverno.github.io/kyverno/ >/dev/null 2>&1 || true

helm repo update >/dev/null

# ---------------------------------------------------------------------------
# Monitoring
# ---------------------------------------------------------------------------

log "monitoring: kube-prometheus-stack $KPS_CHART"

kubectl create namespace monitoring \
  --dry-run=client \
  -o yaml | kubectl apply -f - >/dev/null

if ! kubectl -n monitoring get secret grafana-admin >/dev/null 2>&1; then
  kubectl -n monitoring create secret generic grafana-admin \
    --from-literal=admin-user=admin \
    --from-literal=admin-password="$(rand)" >/dev/null
fi

echo
echo "Installing kube-prometheus-stack..."
echo "Chart version: $KPS_CHART"
echo "Namespace: monitoring"
echo

# Do NOT hide Helm output here.
# If installation fails, the actual Helm error will be visible.
if ! helm upgrade --install kube-prometheus-stack \
  prometheus-community/kube-prometheus-stack \
  --version "$KPS_CHART" \
  -n monitoring \
  -f deploy/observability/kube-prometheus-stack-values.yaml \
  --wait \
  --timeout 10m
then
  echo
  echo "============================================================"
  echo "ERROR: kube-prometheus-stack installation failed"
  echo "============================================================"
  echo

  echo "---- Helm status ----"
  helm -n monitoring status kube-prometheus-stack || true

  echo
  echo "---- Monitoring pods ----"
  kubectl get pods -n monitoring -o wide || true

  echo
  echo "---- Monitoring jobs ----"
  kubectl get jobs -n monitoring || true

  echo
  echo "---- Monitoring events ----"
  kubectl get events \
    -n monitoring \
    --sort-by=.lastTimestamp | tail -80 || true

  echo
  echo "---- Monitoring deployments ----"
  kubectl get deployments -n monitoring || true

  echo
  echo "---- Monitoring statefulsets ----"
  kubectl get statefulsets -n monitoring || true

  echo
  echo "---- Monitoring services ----"
  kubectl get services -n monitoring || true

  echo
  echo "============================================================"
  echo "Fix the monitoring problem above and run:"
  echo
  echo "    make up"
  echo "============================================================"

  exit 1
fi

echo
echo "kube-prometheus-stack installed successfully."
echo

# ---------------------------------------------------------------------------
# Ingress
# ---------------------------------------------------------------------------

log "ingress-nginx $INGRESS_NGINX_CHART"

helm upgrade --install ingress-nginx \
  ingress-nginx/ingress-nginx \
  --version "$INGRESS_NGINX_CHART" \
  -n ingress-nginx \
  --create-namespace \
  -f deploy/nginx/ingress-nginx-values.yaml \
  --wait \
  --timeout 5m >/dev/null

# ---------------------------------------------------------------------------
# Namespace + secrets
# ---------------------------------------------------------------------------

log "namespace + secrets (generated, never printed)"

kubectl apply \
  -f deploy/k8s/base/namespace.yaml >/dev/null

if ! kubectl -n settle get secret postgres-admin >/dev/null 2>&1; then

  PG_ADMIN=$(rand)
  OWNER=$(rand)
  APP=$(rand)
  EXPORTER=$(rand)

  kubectl -n settle create secret generic postgres-admin \
    --from-literal=POSTGRES_PASSWORD="$PG_ADMIN" \
    --from-literal=SETTLE_OWNER_PASSWORD="$OWNER" \
    --from-literal=SETTLE_APP_PASSWORD="$APP" \
    --from-literal=EXPORTER_PASSWORD="$EXPORTER" >/dev/null

  kubectl -n settle create secret generic settle-db \
    --from-literal=DATABASE_URL="postgresql+psycopg://settle_app:${APP}@postgres.settle.svc:5432/settle" >/dev/null

  kubectl -n settle create secret generic settle-db-migrate \
    --from-literal=DATABASE_URL="postgresql+psycopg://settle_owner:${OWNER}@postgres.settle.svc:5432/settle" >/dev/null

  kubectl -n monitoring create secret generic postgres-exporter \
    --from-literal=DATA_SOURCE_NAME="postgresql://exporter:${EXPORTER}@postgres-db.settle.svc:5432/settle?sslmode=disable" \
    --dry-run=client \
    -o yaml | kubectl apply -f - >/dev/null

  unset PG_ADMIN OWNER APP EXPORTER
fi

# ---------------------------------------------------------------------------
# Bank mock image
# ---------------------------------------------------------------------------

log "bank mock image"

docker build \
  -q \
  -f app/Dockerfile \
  -t "$REGISTRY_HOST/settle-bankmock:local" \
  . >/dev/null

docker push \
  -q \
  "$REGISTRY_HOST/settle-bankmock:local" >/dev/null

# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------

log "dependencies: postgres (via toxiproxy), redis, bankmock"

kubectl apply \
  -f local/k8s/ >/dev/null

kubectl -n settle rollout status \
  statefulset/postgres \
  --timeout=180s >/dev/null

for d in toxiproxy redis bankmock; do
  kubectl -n settle rollout status \
    "deployment/$d" \
    --timeout=180s >/dev/null
done

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

log "database at schema 0007 with v1.7 data (as production before v1.8)"

until kubectl -n settle exec postgres-0 -- \
  pg_isready -U postgres -d settle >/dev/null 2>&1
do
  sleep 2
done

psql_owner() {
  kubectl -n settle exec -i postgres-0 -- \
    psql -q \
      -v ON_ERROR_STOP=1 \
      -U settle_owner \
      -d settle \
      "$@"
}

if [ "$(kubectl -n settle exec postgres-0 -- \
  psql -tAq \
    -U settle_owner \
    -d settle \
    -c "SELECT to_regclass('public.payouts') IS NOT NULL")" != "t" ]; then

  psql_owner < migrations/0007_settlements_payouts.sql
  psql_owner < local/seed.sql
fi

# ---------------------------------------------------------------------------
# Image signing + Kyverno
# ---------------------------------------------------------------------------

log "image signing: pipeline key pair + Kyverno admission policy (Kyverno $KYVERNO_CHART)"

mkdir -p local/.secrets
chmod 700 local/.secrets

if [ ! -f local/.secrets/cosign.key ]; then

  (
    cd local/.secrets

    umask 077

    openssl rand -hex 24 > cosign.password

    COSIGN_PASSWORD="$(cat cosign.password)" \
      cosign generate-key-pair >/dev/null 2>&1
  )
fi

helm upgrade --install kyverno \
  kyverno/kyverno \
  --version "$KYVERNO_CHART" \
  -n kyverno \
  --create-namespace \
  -f deploy/policy/kyverno-values.yaml \
  --wait \
  --timeout 15m >/dev/null

scripts/render-policy.sh \
  local/.secrets/cosign.pub |
  kubectl apply -f - 2>&1 |
  grep -v "^Warning" >/dev/null

# ---------------------------------------------------------------------------
# PostgreSQL exporter + monitoring configuration
# ---------------------------------------------------------------------------

log "postgres-exporter, monitors, SLO rules, dashboard"

helm upgrade --install postgres-exporter \
  prometheus-community/prometheus-postgres-exporter \
  --version "$PG_EXPORTER_CHART" \
  -n monitoring \
  -f deploy/observability/postgres-exporter-values.yaml \
  --wait \
  --timeout 5m >/dev/null

kubectl apply \
  -f deploy/observability/monitors.yaml \
  -f deploy/observability/rules/ \
  -f deploy/observability/dashboards/ >/dev/null

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------

log "done"

cat <<EOF

  settle API (after first deploy): http://settle.localtest.me:8088/settlements

  Grafana:
    make grafana

  Grafana password:
    make grafana-password

  Prometheus:
    make prometheus

  Alertmanager:
    make alertmanager

  Next:
    make deploy VERSION=1.9.0

EOF
```
