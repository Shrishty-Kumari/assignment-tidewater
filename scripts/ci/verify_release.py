#!/usr/bin/env python3
"""Post-deploy verification: decides whether a release stays or is rolled back.

"The rollout finished" only means the pods pass their probes. That is not
the same as "the release works" (probes are deliberately shallow, see
docs/DECISIONS.md ADR-003). This script checks what users and finance see:

  1. Synthetic journeys through the ingress, for DURATION seconds, at a steady
     rate: list settlements, create one, execute it, read it back, and follow
     one payout end to end until the worker has paid it via the bank.
  2. Prometheus, scoped to the NEW version's own metrics:
       - 5xx ratio of settle_http_requests_total{version=NEW}
       - p95 latency of the same requests
       - container restarts in the namespace during the window
       - duplicate payouts reported by reconciliation
  3. A verdict table. Any failed gate -> exit 1 -> the pipeline rolls back.

stdlib only, so it runs on any runner (python3 + kubectl).

    scripts/ci/verify_release.py --version 1.9.0 [--duration 90]
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import random
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

INGRESS = os.getenv("INGRESS_URL", "http://settle.localtest.me:8088")
HOST = os.getenv("INGRESS_HOST", "settle.localtest.me")
NS = os.getenv("NAMESPACE", "settle")

# release gates
MAX_5XX_RATIO = 0.01
# transport errors (no HTTP response at all) come from the runner's network
# path, e.g. act -> host.docker.internal; tracked separately, looser bound
MAX_TRANSPORT_ERROR_RATIO = 0.05
MAX_P95_SECONDS = 0.5
MAX_RESTARTS = 0
PAYOUT_DEADLINE_S = 60


def http(method, path, body=None, timeout=15):  # > nginx's 10 s, so nginx's 504 wins
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(INGRESS + path, data=data, method=method,
                                 headers={"Host": HOST, "Content-Type": "application/json",
                                          "X-Request-ID": f"verify-{random.getrandbits(48):012x}"})
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"null"), time.monotonic() - t0
    except urllib.error.HTTPError as e:
        return e.code, None, time.monotonic() - t0
    except Exception:
        return 599, None, time.monotonic() - t0  # connection error / timeout


PROM_PROXY = os.getenv(
    "PROM_PROXY", "/api/v1/namespaces/monitoring/services/kube-prometheus-stack-prometheus:9090/proxy")


def prom(query):
    """Query Prometheus through the Kubernetes API service proxy (no port-forward,
    no exec). Returns None when there is no data or Prometheus is unreachable;
    every gate treats None as a failure (fail closed)."""
    path = f"{PROM_PROXY}/api/v1/query?" + urllib.parse.urlencode({"query": query})
    out = subprocess.run(["kubectl", "get", "--raw", path], capture_output=True, text=True, timeout=30)
    if out.returncode != 0:
        print(f"  prometheus query failed: {out.stderr.strip()[:200]}", file=sys.stderr)
        return None
    res = json.loads(out.stdout)["data"]["result"]
    return float(res[0]["value"][1]) if res else None


def journeys(duration, rps):
    stats = {"requests": 0, "5xx": 0, "transport": 0, "by_check": {}, "sample_ids": []}

    def record(name, status):
        stats["requests"] += 1
        c = stats["by_check"].setdefault(name, {"ok": 0, "fail": 0, "codes": {}})
        ok = status < 500
        c["ok" if ok else "fail"] += 1
        c["codes"][str(status)] = c["codes"].get(str(status), 0) + 1
        if status == 599:
            stats["transport"] += 1
        elif not ok:
            stats["5xx"] += 1
        return ok

    end = time.monotonic() + duration
    date = (dt.date.today() + dt.timedelta(days=random.randint(1000, 5000))).isoformat()
    while time.monotonic() < end:
        tick = time.monotonic()
        s, _, _ = http("GET", "/settlements?limit=20")
        record("GET /settlements", s)
        merchant = random.randint(1, 500)
        s, body, _ = http("POST", "/settlements",
                          {"merchant_id": merchant, "settlement_date": date, "amount_minor": random.randint(100, 99999)})
        if record("POST /settlements", s) and body:
            sid = body["id"]
            s, _, _ = http("GET", f"/settlements/{sid}")
            record("GET /settlements/{id}", s)
            if len(stats["sample_ids"]) < 3:
                s, _, _ = http("POST", f"/settlements/{sid}/execute")
                if record("POST /settlements/{id}/execute", s):
                    stats["sample_ids"].append(sid)
        date = (dt.date.fromisoformat(date) + dt.timedelta(days=1)).isoformat()
        time.sleep(max(0.0, 1.0 / rps - (time.monotonic() - tick)))
    return stats


def payout_paid(ids):
    """End to end: API -> queue -> worker -> bank -> DB."""
    deadline = time.monotonic() + PAYOUT_DEADLINE_S
    pending = set(ids)
    while pending and time.monotonic() < deadline:
        for sid in list(pending):
            s, body, _ = http("GET", f"/settlements/{sid}")
            if s == 200 and body and body.get("payout_state") == "paid":
                pending.discard(sid)
        time.sleep(2)
    return len(ids) - len(pending), len(ids)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--version", required=True)
    ap.add_argument("--duration", type=int, default=int(os.getenv("VERIFY_DURATION", "90")))
    ap.add_argument("--rps", type=float, default=3.0)
    args = ap.parse_args()
    v = args.version

    print(f"verifying settle {v}: {args.duration}s of synthetic traffic via {INGRESS} (Host: {HOST})", flush=True)
    restarts_before = prom(f'sum(kube_pod_container_status_restarts_total{{namespace="{NS}"}})')
    started = time.time()
    stats = journeys(args.duration, args.rps)
    paid, executed = payout_paid(stats["sample_ids"])
    time.sleep(20)  # let Prometheus scrape the tail of the window
    # the window covers the whole verification (traffic + payout follow-up),
    # not a fixed tail that would over-weight whatever ran last
    window = f"{int(time.time() - started) + 15}s"

    # no 5xx series at all means zero errors (or vector(0)); no request series
    # at all means the new version served nothing, which must fail
    ratio = prom(f'(sum(increase(settle_http_requests_total{{version="{v}",status=~"5.."}}[{window}])) or vector(0))'
                 f' / sum(increase(settle_http_requests_total{{version="{v}"}}[{window}]))')
    p95 = prom(f'histogram_quantile(0.95, sum by (le) (rate(settle_http_request_duration_seconds_bucket'
               f'{{version="{v}",route!="/reports/daily"}}[{window}])))')
    restarts_after = prom(f'sum(kube_pod_container_status_restarts_total{{namespace="{NS}"}})')
    restarts = None if restarts_before is None or restarts_after is None else restarts_after - restarts_before
    dups = prom("max(settle_reconciliation_duplicate_payouts)")
    client_ratio = stats["5xx"] / max(1, stats["requests"])
    transport_ratio = stats["transport"] / max(1, stats["requests"])

    gates = [
        ("synthetic journeys: HTTP 5xx ratio", f"{client_ratio:.1%}", f"<= {MAX_5XX_RATIO:.0%}",
         client_ratio <= MAX_5XX_RATIO),
        ("synthetic journeys: transport errors (no response)", f"{transport_ratio:.1%}",
         f"<= {MAX_TRANSPORT_ERROR_RATIO:.0%}", transport_ratio <= MAX_TRANSPORT_ERROR_RATIO),
        (f"end-to-end payout paid within {PAYOUT_DEADLINE_S}s", f"{paid}/{executed}", "all",
         executed > 0 and paid == executed),
        (f"prometheus 5xx ratio, version={v}", "no data" if ratio is None else f"{ratio:.1%}", f"<= {MAX_5XX_RATIO:.0%}",
         ratio is not None and ratio <= MAX_5XX_RATIO),
        (f"prometheus p95 latency, version={v}", "no data" if p95 is None else f"{p95 * 1000:.0f} ms",
         f"<= {MAX_P95_SECONDS * 1000:.0f} ms", p95 is not None and p95 <= MAX_P95_SECONDS),
        ("container restarts during window", "no data" if restarts is None else f"{restarts:.0f}", f"<= {MAX_RESTARTS}",
         restarts is not None and restarts <= MAX_RESTARTS),
        ("duplicate payouts (reconciliation)", "no data" if dups is None else f"{dups:.0f}", "0",
         dups is not None and dups == 0),
    ]
    print("\nper-check results (synthetic):")
    for name, c in stats["by_check"].items():
        print(f"  {name:<34} ok={c['ok']:<4} fail={c['fail']:<4} codes={c['codes']}")
    print(f"\n{'gate':<52}{'observed':>12}  {'threshold':<10} result")
    for name, obs, thr, ok in gates:
        print(f"{name:<52}{obs:>12}  {thr:<10} {'PASS' if ok else 'FAIL'}")
    failed = [g[0] for g in gates if not g[3]]
    summary = {"version": v, "passed": not failed, "failed_gates": failed}
    print("\n" + json.dumps(summary))
    if os.getenv("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
            f.write(f"### Post-deploy verification of {v}: {'PASS' if not failed else 'FAIL'}\n\n| gate | observed | threshold | result |\n|---|---|---|---|\n")
            for name, obs, thr, ok in gates:
                f.write(f"| {name} | {obs} | {thr} | {'PASS' if ok else 'FAIL'} |\n")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
