#!/usr/bin/env bash
set -euo pipefail
NS=settle
TOXI="kubectl -n $NS exec deploy/toxiproxy -- /toxiproxy-cli"
URL="${INGRESS_URL:-http://settle.localtest.me:8088}"

observe() {
  local secs="${1:-60}" codes restarts conns
  echo "observing for ${secs}s: status codes of GET /settlements, restarts, DB connections"
  restarts=$(kubectl -n $NS get pods -o jsonpath='{range .items[*]}{.status.containerStatuses[0].restartCount}{"\n"}{end}' | paste -sd+ - | bc)
  codes=$(for _ in $(seq 1 "$secs"); do
            curl -s -o /dev/null -m 12 -w '%{http_code} %{time_total}\n' -H "Host: settle.localtest.me" "$URL/settlements?limit=5" &
            sleep 1
          done; wait)
  echo "$codes" | awk '{c[$1]++; t[$1]+=$2} END {for (k in c) printf "  HTTP %s: %d requests, avg %.2fs\n", k, c[k], t[k]/c[k]}'
  conns=$(kubectl -n $NS exec postgres-0 -- psql -tAq -U postgres -d settle -c "select count(*) from pg_stat_activity where datname='settle'")
  echo "  postgres connections now: $conns / 100"
  echo "  container restarts before: $restarts, after: $(kubectl -n $NS get pods -o jsonpath='{range .items[*]}{.status.containerStatuses[0].restartCount}{"\n"}{end}' | paste -sd+ - | bc)"
}

case "${1:-}" in
  on)
    $TOXI toxic add -t latency -a latency=3000 -n pg_latency postgres >/dev/null
    echo "chaos ON: +3000 ms on every Postgres response (toxiproxy)"
    observe 60
    ;;
  off)
    $TOXI toxic remove -n pg_latency postgres >/dev/null || true
    echo "chaos OFF"
    observe 20
    ;;
  observe)
    observe "${2:-30}"
    ;;
  *) echo "usage: $0 on|off|observe [seconds]"; exit 2 ;;
esac
