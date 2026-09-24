#!/usr/bin/env python3
"""Fail if the worst-case Postgres connection demand exceeds what Postgres accepts.

    kubectl kustomize deploy/k8s/overlays/local | scripts/check_db_budget.py -

Formula (docs/CHANGES.md, "Connection budget"):

  demand = sum over Deployments d that use the DB of
             pods_max(d) x processes(d) x (DB_POOL_SIZE(d) + DB_MAX_OVERFLOW(d))
           + reserved (migrate Job, exporter)

  pods_max(d) = maxReplicas (HPA) or replicas
              + maxSurge                 (extra pod during a rollout)
              + 1                        (a terminating pod still holds its pool
                                          for up to terminationGracePeriodSeconds)

  budget = max_connections - superuser_reserved_connections - admin_headroom

All inputs are read from the rendered manifests, so changing replicas, the
HPA, gunicorn workers or pool sizes without re-checking is impossible.
On 14 Aug the same formula gives 10 x 4 x (5 + 10) + 4 x 10 = 640 against 97.
"""

from __future__ import annotations

import argparse
import math
import sys

import yaml


def load(stream):
    return [d for d in yaml.safe_load_all(stream) if d]


def resolve(value, total):
    if isinstance(value, str) and value.endswith("%"):
        return math.ceil(total * int(value[:-1]) / 100)
    return int(value)


def env_value(container, name, configmaps):
    for e in container.get("env", []):
        if e["name"] != name:
            continue
        if "value" in e:
            return e["value"]
        ref = e.get("valueFrom", {}).get("configMapKeyRef")
        if ref:
            return configmaps[ref["name"]][ref["key"]]
    for src in container.get("envFrom", []):
        ref = src.get("configMapRef")
        if ref and name in configmaps.get(ref["name"], {}):
            return configmaps[ref["name"]][name]
    return None


def uses_db(container):
    return any(e["name"] == "DATABASE_URL" for e in container.get("env", []))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("manifests", help="rendered manifests file, or - for stdin")
    ap.add_argument("--max-connections", type=int, default=100)
    ap.add_argument("--superuser-reserved", type=int, default=3)
    ap.add_argument("--admin-headroom", type=int, default=5,
                    help="kept free for humans (psql) and emergency tooling")
    ap.add_argument("--reserved", type=int, default=3,
                    help="migrate Job (2: lock + work) + postgres-exporter (1)")
    args = ap.parse_args()

    docs = load(sys.stdin if args.manifests == "-" else open(args.manifests))
    configmaps = {d["metadata"]["name"]: d.get("data", {}) for d in docs if d["kind"] == "ConfigMap"}
    hpas = {d["spec"]["scaleTargetRef"]["name"]: d for d in docs if d["kind"] == "HorizontalPodAutoscaler"}

    rows, demand = [], args.reserved
    for d in (x for x in docs if x["kind"] == "Deployment"):
        name = d["metadata"]["name"]
        c = d["spec"]["template"]["spec"]["containers"][0]
        if not uses_db(c):
            continue
        base = hpas[name]["spec"]["maxReplicas"] if name in hpas else d["spec"].get("replicas", 1)
        ru = d["spec"].get("strategy", {}).get("rollingUpdate", {})
        surge = resolve(ru.get("maxSurge", "25%"), base)
        pods = base + surge + 1
        # gunicorn (no command override) runs GUNICORN_WORKERS processes
        procs = int(env_value(c, "GUNICORN_WORKERS", configmaps) or 1) if not c.get("command") else 1
        pool = int(env_value(c, "DB_POOL_SIZE", configmaps) or 5)
        overflow = int(env_value(c, "DB_MAX_OVERFLOW", configmaps) or 10)
        conns = pods * procs * (pool + overflow)
        demand += conns
        rows.append((name, base, surge, pods, procs, pool, overflow, conns))

    budget = args.max_connections - args.superuser_reserved - args.admin_headroom
    print(f"{'deployment':<16}{'max':>5}{'surge':>7}{'pods':>6}{'procs':>7}{'pool':>6}{'ovf':>5}{'conns':>7}")
    for r in rows:
        print(f"{r[0]:<16}{r[1]:>5}{r[2]:>7}{r[3]:>6}{r[4]:>7}{r[5]:>6}{r[6]:>5}{r[7]:>7}")
    print(f"{'reserved':<16}{'':>36}{args.reserved:>7}")
    print(f"demand {demand} vs budget {budget} "
          f"(= {args.max_connections} max_connections - {args.superuser_reserved} superuser - {args.admin_headroom} admin)")
    if demand > budget:
        print(f"::error::DB connection budget exceeded by {demand - budget}")
        return 1
    print(f"OK, headroom {budget - demand}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
