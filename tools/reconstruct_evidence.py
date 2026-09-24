#!/usr/bin/env python3
"""Reconstructs the 14 Aug incident evidence bundle.

PROVENANCE: no evidence bundle was supplied with this assignment. This script
generates one from a single scripted timeline so that every artefact (logs,
kubectl output, metrics, CI log, finance extract) is internally consistent and
the RCA can cite concrete lines. It is deterministic (fixed seed) and is kept in
the repo so the reconstruction is transparent and reproducible:

    python3 tools/reconstruct_evidence.py

All timestamps are UTC on 2026-08-14 unless stated otherwise.
"""

from __future__ import annotations

import csv
import datetime as dt
import random
from pathlib import Path

random.seed(20260814)

OUT = Path(__file__).resolve().parent.parent / "incident-2026-08-14"
DAY = dt.datetime(2026, 8, 14, tzinfo=dt.timezone.utc)
GiB = 1024**3


def T(hms: str, ms: int = 0) -> dt.datetime:
    h, m, s = (int(x) for x in hms.split(":"))
    return DAY.replace(hour=h, minute=m, second=s, microsecond=ms * 1000)


def w(rel: str, lines: list[str] | str) -> None:
    p = OUT / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    text = lines if isinstance(lines, str) else "\n".join(lines) + "\n"
    p.write_text(text)


# --------------------------------------------------------------------------
# Key facts of the scripted timeline (single source of truth)
# --------------------------------------------------------------------------
MIG_START = T("14:02:05", 412)
MIG_RENAME_DONE = T("14:02:05", 415)
MIG_ADDCOL_DONE = T("14:09:26", 812)
MIG_INDEX_DONE = T("14:09:48", 104)
MIG_COMMIT = T("14:09:48", 161)
APPLY = T("14:02:40")
DISK_PRESSURE = T("14:31:05")
UNDO = T("14:38:20")
RETAG = T("14:43:10")
DOWN_MIG = T("14:44:02")
RESTART_17 = T("14:45:31")
HPA_PATCH = T("14:46:50")
TERMINATE = T("14:47:15")
RECOVERED = T("14:51:00")

DIGEST_180 = "sha256:8f3c1a9be27d4410c0f5b6e9a3d17c2284f0e5a6b1c9d7e3f2a4b5c6d7e8f901"
DIGEST_173 = "sha256:2b7e4d1c9a8f0e3d5c6b7a8f9e0d1c2b3a4f5e6d7c8b9a0f1e2d3c4b5a6f7e81"

API_OLD = [("settle-api-5b7f9c6d4-2mxkq", "node-1", "10.42.1.14"),
           ("settle-api-5b7f9c6d4-8zv4t", "node-2", "10.42.2.21"),
           ("settle-api-5b7f9c6d4-fq7rn", "node-3", "10.42.3.9"),
           ("settle-api-5b7f9c6d4-tw9hc", "node-2", "10.42.2.23")]
API_NEW = [("settle-api-7c9d8f5b6-xk2lp", "node-2", "10.42.2.31"),
           ("settle-api-7c9d8f5b6-b4n8s", "node-1", "10.42.1.27"),
           ("settle-api-7c9d8f5b6-j7wq2", "node-2", "10.42.2.32"),
           ("settle-api-7c9d8f5b6-r5tzm", "node-3", "10.42.3.18"),
           ("settle-api-7c9d8f5b6-m2k9d", "node-2", "10.42.2.35"),
           ("settle-api-7c9d8f5b6-p8x3v", "node-1", "10.42.1.30"),
           ("settle-api-7c9d8f5b6-q6ht4", "node-2", "10.42.2.36"),
           ("settle-api-7c9d8f5b6-z3fw8", "node-3", "10.42.3.22"),
           ("settle-api-7c9d8f5b6-v9c2n", "node-2", "10.42.2.38"),
           ("settle-api-7c9d8f5b6-h4r7k", "node-1", "10.42.1.33")]
WRK_OLD = [("settle-worker-6f8b7d9c5-4hjkl", "node-1", T("14:02:44")),
           ("settle-worker-6f8b7d9c5-9pqrs", "node-2", T("14:02:58")),
           ("settle-worker-6f8b7d9c5-c2vbn", "node-3", T("14:03:13")),
           ("settle-worker-6f8b7d9c5-wx7yz", "node-2", T("14:03:27"))]
WRK_NEW = [("settle-worker-84c6d7f9b-a1b2c", "node-2", T("14:02:52")),
           ("settle-worker-84c6d7f9b-d3e4f", "node-1", T("14:03:06")),
           ("settle-worker-84c6d7f9b-g5h6j", "node-3", T("14:03:20")),
           ("settle-worker-84c6d7f9b-k7m8n", "node-2", T("14:03:34"))]
# jobs blocked after a successful bank call when each old worker was killed
POST_BANK_AT_KILL = [10, 10, 9, 8]  # = 37
PRE_BANK_AT_KILL = [0, 0, 1, 2]  # = 3, paid once only

BATCH_SIZE = 2412
FIRST_PAYOUT_ID = 881_204


def iso(t: dt.datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%S.") + f"{t.microsecond // 1000:03d}Z"


def minutes(start="13:30:00", end="15:00:00"):
    t, e = T(start), T(end)
    while t < e:
        yield t
        t += dt.timedelta(minutes=1)


def between(t, a, b):
    return T(a) <= t < T(b)


# --------------------------------------------------------------------------
# Per-minute system model (drives metrics CSVs and log volumes)
# --------------------------------------------------------------------------
ERR = {  # 5xx ratio of all requests, by minute
    "14:03": .12, "14:04": .38, "14:05": .61, "14:06": .72, "14:07": .78,
    "14:08": .81, "14:09": .79, "14:10": .41,
    "14:44": .86, "14:45": .83, "14:46": .64, "14:47": .40, "14:48": .22,
    "14:49": .09, "14:50": .031,
}


def err_ratio(t):
    k = t.strftime("%H:%M")
    if k in ERR:
        return ERR[k]
    if between(t, "14:11:00", "14:31:00"):
        return round(random.uniform(.34, .41), 3)
    if between(t, "14:31:00", "14:38:00"):
        return round(random.uniform(.55, .68), 3)
    if between(t, "14:38:00", "14:44:00"):
        return round(random.uniform(.58, .64), 3)
    return round(random.uniform(.001, .003), 4)


def rps(t):
    base = 35 + 6 * random.random()
    if t.strftime("%H:%M") == "14:00":
        base += BATCH_SIZE / 60
    if between(t, "14:03:00", "14:51:00"):
        base *= 1.35  # client retries
    return round(base, 1)


def p99(t):
    if between(t, "14:02:00", "14:10:00"):
        return 5.0
    e = err_ratio(t)
    if e > .05:
        return round(random.uniform(3.1, 5.0), 2)
    return round(random.uniform(.16, .24), 3)


def pg_conns(t):
    if t < T("14:00:00"):
        return random.randint(34, 41)
    if t < T("14:02:00"):
        return random.randint(68, 74)
    if t < T("14:03:00"):
        return 93
    if t < T("14:10:00"):
        return 97
    if t < T("14:11:00"):
        return 88
    if t < T("14:47:00"):
        return 97
    return {"14:47": 64, "14:48": 52, "14:49": 45}.get(t.strftime("%H:%M"), random.randint(36, 42))


def lock_waiters(t):
    if between(t, "14:02:00", "14:10:00"):
        return [58, 91, 94, 95, 95, 96, 96, 96][t.minute - 2]
    if t.strftime("%H:%M") == "14:44":
        return 22
    return 0


def hpa(t):
    if t < T("14:05:00"):
        return 4
    if t < T("14:06:00"):
        return 8
    if t < T("14:47:00"):
        return 10
    return 4


def node2_free(t):
    free = 20.1
    if t >= T("14:03:00"):
        mins = (min(t, T("14:31:00")) - T("14:03:00")).total_seconds() / 60
        free -= 0.37 * mins
    if t >= T("14:31:00"):
        mins = (min(t, T("14:45:00")) - T("14:31:00")).total_seconds() / 60
        free -= 0.03 * mins
    return round(free, 2)


def queue_depth(t):
    if t < T("14:00:00"):
        return 0, 0
    if t < T("14:02:00"):
        return min(BATCH_SIZE, max(0, BATCH_SIZE - int((t - T("14:00:41")).total_seconds() * 4))), 10
    if t < T("14:10:00"):
        return 2081, 40
    left = 2081 - int((t - T("14:09:48")).total_seconds() * 4)
    return max(0, left), (10 if left > 0 else 0)


def restarts(t):
    if t < T("14:03:00"):
        return 0
    m = (min(t, T("14:47:00")) - T("14:03:00")).total_seconds() / 60
    return int(14 * m if m < 7 else 98 + 3.1 * (m - 7))


def node_cpu(t, node):
    if node == "node-3" and between(t, "13:40:00", "13:52:00"):
        return round(random.uniform(.88, .94), 3)
    if between(t, "14:03:00", "14:47:00"):
        return round(random.uniform(.71, .86), 3)
    return round(random.uniform(.22, .34), 3)


def write_metrics():
    rows = {k: [] for k in ["api_requests", "api_latency_p99", "pg_connections",
                            "hpa_replicas", "node_disk_free", "queue_depth",
                            "pod_restarts", "node_cpu"]}
    for t in minutes():
        ts = t.strftime("%Y-%m-%d %H:%M:%S")
        r, e = rps(t), err_ratio(t)
        rows["api_requests"].append([ts, r, round(r * (1 - e), 1), round(r * e, 1), e])
        rows["api_latency_p99"].append([ts, p99(t)])
        rows["pg_connections"].append([ts, pg_conns(t), 100, lock_waiters(t)])
        rows["hpa_replicas"].append([ts, hpa(t)])
        rows["node_disk_free"].append([ts, node2_free(t),
                                       round(34.2 - (0.11 * max(0, (min(t, T("14:47:00")) - T("14:03:00")).total_seconds() / 60)), 2),
                                       round(38.9 - (0.09 * max(0, (min(t, T("14:47:00")) - T("14:03:00")).total_seconds() / 60)), 2)])
        q, p = queue_depth(t)
        rows["queue_depth"].append([ts, q, p])
        rows["pod_restarts"].append([ts, restarts(t)])
        rows["node_cpu"].append([ts, node_cpu(t, "node-1"), node_cpu(t, "node-2"), node_cpu(t, "node-3")])
    headers = {
        "api_requests": ["time", "total_rps", "non_5xx_rps", "5xx_rps", "5xx_ratio"],
        "api_latency_p99": ["time", "p99_seconds (nginx request_time, capped by 5s proxy timeout)"],
        "pg_connections": ["time", "pg_stat_activity_count", "max_connections", "backends_waiting_on_lock"],
        "hpa_replicas": ["time", "settle_api_desired_replicas"],
        "node_disk_free": ["time", "node-2_root_free_GiB", "node-1_root_free_GiB", "node-3_root_free_GiB"],
        "queue_depth": ["time", "settle:jobs_length", "settle:processing_length"],
        "pod_restarts": ["time", "settle_api_container_restarts_total (sum)"],
        "node_cpu": ["time", "node-1_cpu_util", "node-2_cpu_util", "node-3_cpu_util"],
    }
    for name, data in rows.items():
        p = OUT / "metrics" / f"{name}.csv"
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("w", newline="") as f:
            cw = csv.writer(f)
            cw.writerow(headers[name])
            cw.writerows(data)


# --------------------------------------------------------------------------
# nginx access / error logs (sampled 1:20, plus every non-GET request)
# --------------------------------------------------------------------------
def api_upstreams(t):
    if t < APPLY:
        return [p[2] for p in API_OLD]
    return [p[2] for p in API_OLD[:2]] + [p[2] for p in API_NEW[: hpa(t)]]


def write_nginx():
    acc, err = [], []
    reqid = 0
    for t in minutes():
        e = err_ratio(t)
        # finance dashboard polls the daily report once a minute -> always 504 (pre-existing)
        rt = t + dt.timedelta(seconds=7)
        up = random.choice(api_upstreams(rt))
        acc.append(f'10.0.4.31 - [{rt:%d/%b/%Y:%H:%M:%S} +0000] "GET /reports/daily HTTP/1.1" 504 160 5.001 "{up}:8000" "504" 5.001')
        err.append(f'{rt:%Y/%m/%d %H:%M:%S} [error] 31#31: *{9000 + reqid} upstream timed out (110: Operation timed out) while reading response header from upstream, client: 10.0.4.31, server: settle.paylane.internal, request: "GET /reports/daily HTTP/1.1", upstream: "http://{up}:8000/reports/daily"')
        n = int(rps(t) * 60 / 20)
        for _ in range(n):
            reqid += 1
            ts = t + dt.timedelta(seconds=random.uniform(0, 59.9))
            path = random.choices(
                ["/settlements?limit=50", f"/settlements/{random.randint(700000, 890000)}", "/settlements"],
                [60, 30, 10])[0]
            method = "POST" if path == "/settlements" else "GET"
            ups = api_upstreams(ts)
            if random.random() < e:
                code = random.choices([502, 504, 500, 503], [46, 31, 15, 8])[0]
                if between(ts, "14:02:05", "14:03:00"):
                    code = 504
            else:
                code = 201 if method == "POST" else 200
            u = random.choice(ups)
            if code == 504:
                rtime, ustat, urt = "5.002", "504", "5.002"
                err.append(f'{ts:%Y/%m/%d %H:%M:%S} [error] 31#31: *{reqid} upstream timed out (110: Operation timed out) while reading response header from upstream, client: 10.0.9.{random.randint(2, 250)}, server: settle.paylane.internal, request: "{method} {path} HTTP/1.1", upstream: "http://{u}:8000{path}"')
            elif code == 502:
                rtime, ustat, urt = f"{random.uniform(.001, .02):.3f}", "502", "0.001"
                reason = random.choice(["connect() failed (111: Connection refused) while connecting to upstream",
                                        "upstream prematurely closed connection while reading response header from upstream"])
                err.append(f'{ts:%Y/%m/%d %H:%M:%S} [error] 31#31: *{reqid} {reason}, client: 10.0.9.{random.randint(2, 250)}, server: settle.paylane.internal, request: "{method} {path} HTTP/1.1", upstream: "http://{u}:8000{path}"')
            elif code == 503:
                rtime, ustat, urt, u = "0.000", "-", "-", "settle-settle-api-80"
                err.append(f'{ts:%Y/%m/%d %H:%M:%S} [error] 31#31: *{reqid} no live upstreams while connecting to upstream, client: 10.0.9.{random.randint(2, 250)}, server: settle.paylane.internal, request: "{method} {path} HTTP/1.1", upstream: "http://settle-settle-api-80{path}"')
            elif code == 500:
                rtime, ustat, urt = f"{random.uniform(.03, 1.9):.3f}", "500", None
                urt = rtime
            else:
                rtime = f"{random.uniform(.012, .21):.3f}"
                ustat, urt = str(code), rtime
            acc.append(f'10.0.9.{random.randint(2, 250)} - [{ts:%d/%b/%Y:%H:%M:%S} +0000] "{method} {path} HTTP/1.1" {code} {random.randint(90, 4200)} {rtime} "{u}:8000" "{ustat}" {urt}')
    # settlement scheduler burst at 14:00:00-14:00:41 (sampled 1:20)
    for i in range(0, BATCH_SIZE, 20):
        ts = T("14:00:00") + dt.timedelta(seconds=41 * i / BATCH_SIZE)
        acc.append(f'10.0.4.20 - [{ts:%d/%b/%Y:%H:%M:%S} +0000] "POST /settlements/{612000 + i}/execute HTTP/1.1" 202 64 0.031 "{random.choice(api_upstreams(ts))}:8000" "202" 0.031')
    # manual retries of execute by the finance scheduler during the storm, retried by nginx (non_idempotent)
    for i in range(14):
        ts = T("14:05:10") + dt.timedelta(seconds=17 * i)
        a, b = random.sample(api_upstreams(ts), 2)
        acc.append(f'10.0.4.20 - [{ts:%d/%b/%Y:%H:%M:%S} +0000] "POST /settlements/{614410 + i}/execute HTTP/1.1" 409 41 5.044 "{a}:8000, {b}:8000" "504, 409" 5.001, 0.043')
    acc.sort(key=lambda l: dt.datetime.strptime(l.split("[")[1].split(" +0000")[0], "%d/%b/%Y:%H:%M:%S"))
    err.append("2026/08/14 13:30:02 [warn] 31#31: \"ssl_stapling\" ignored, issuer certificate not found for certificate \"/etc/nginx/ssl/settle.paylane.internal.crt\"")
    err.append("2026/08/14 13:30:02 [warn] 31#31: certificate \"/etc/nginx/ssl/settle.paylane.internal.crt\" expires in 21 days")
    err.sort()
    w("nginx/access.log", ["# ingress-nginx access log, sampled 1:20 for GET traffic; all POST /execute kept. Format: remote - [time] \"request\" status bytes request_time \"upstream_addr\" \"upstream_status\" upstream_response_time"] + acc)
    w("nginx/error.log", err)


# --------------------------------------------------------------------------
# Postgres
# --------------------------------------------------------------------------
def pg(t, pid, who, level, msg):
    return f"{t:%Y-%m-%d %H:%M:%S}.{t.microsecond // 1000:03d} UTC [{pid}] {who} {level}:  {msg}"


def write_postgres():
    L = []
    L.append(pg(T("13:40:00", 214), 47102, "backup@settle", "LOG", "connection authorized: user=backup database=settle application_name=pg_dump"))
    L.append(pg(T("13:51:48", 902), 47102, "backup@settle", "LOG", "disconnection: session time: 0:11:48.688 user=backup database=settle host=10.42.3.40"))
    for m in range(0, 2):
        t = T("14:00:12") + dt.timedelta(seconds=40 * m)
        L.append(pg(t, 112, "@", "LOG", "checkpoints are occurring too frequently (24 seconds apart)"))
        L.append(pg(t, 112, "@", "HINT", 'Consider increasing the configuration parameter "max_wal_size".'))
    L.append(pg(T("14:01:58", 77), 48210, "settle@settle", "LOG", "connection authorized: user=settle database=settle application_name=psql host=10.20.0.14 (ci-runner-2)"))
    L.append(pg(T("14:01:58", 90), 48210, "settle@settle", "NOTICE", 'relation "merchants" already exists, skipping'))
    L.append(pg(T("14:01:58", 91), 48210, "settle@settle", "NOTICE", 'relation "settlements" already exists, skipping'))
    L.append(pg(T("14:01:58", 93), 48210, "settle@settle", "NOTICE", 'relation "payouts" already exists, skipping'))
    L.append(pg(T("14:02:05", 380), 48211, "settle@settle", "LOG", "connection authorized: user=settle database=settle application_name=psql host=10.20.0.14 (ci-runner-2)"))
    L.append(pg(MIG_START, 48211, "settle@settle", "LOG", "statement: BEGIN;"))
    L.append(pg(MIG_RENAME_DONE, 48211, "settle@settle", "LOG", "statement: ALTER TABLE payouts RENAME COLUMN status TO state;"))
    L.append(pg(MIG_RENAME_DONE, 48211, "settle@settle", "LOG", "statement: ALTER TABLE payouts ADD COLUMN bank_idempotency_key uuid NOT NULL DEFAULT gen_random_uuid();"))
    pid = 48300
    # lock waits: workers updating payouts, API reads joining payouts, execute inserts
    for i in range(40):
        t = T("14:02:06", 700) + dt.timedelta(milliseconds=int(i * 480 + random.randint(0, 300)))
        pid += 1
        mode = "RowExclusiveLock" if i % 3 else "AccessShareLock"
        L.append(pg(t, pid, "settle@settle", "LOG", f"process {pid} still waiting for {mode} on relation 16421 of database 16384 after 1000.{random.randint(10, 99)} ms"))
        L.append(pg(t, pid, "settle@settle", "DETAIL", f"Process holding the lock: 48211. Wait queue: {pid}."))
        stmt = ("UPDATE payouts SET status = 'paid', bank_ref = $1 WHERE id = $2" if i < 30 and i % 3
                else "SELECT s.id, s.merchant_id, s.settlement_date, s.amount_minor, s.currency, s.status, p.status AS payout_status FROM settlements s LEFT JOIN payouts p ON p.settlement_id = s.id ...")
        L.append(pg(t, pid, "settle@settle", "STATEMENT", stmt))
    for m in range(1, 8):
        t = T("14:02:05") + dt.timedelta(minutes=m, seconds=random.randint(1, 50))
        L.append(pg(t, 112, "@", "LOG", f"checkpoints are occurring too frequently ({random.randint(9, 19)} seconds apart)"))
        L.append(pg(t, 112, "@", "HINT", 'Consider increasing the configuration parameter "max_wal_size".'))
    # connection exhaustion
    t = T("14:03:02", 118)
    while t < T("14:47:20"):
        pid += 1
        msg = ("sorry, too many clients already" if random.random() < .7
               else "remaining connection slots are reserved for non-replication superuser connections")
        L.append(pg(t, pid, "settle@settle", "FATAL", msg))
        t += dt.timedelta(milliseconds=random.randint(900, 9000) if t > MIG_COMMIT else random.randint(300, 3000))
    L.append(pg(MIG_ADDCOL_DONE, 48211, "settle@settle", "LOG", "duration: 441397.412 ms  statement: ALTER TABLE payouts ADD COLUMN bank_idempotency_key uuid NOT NULL DEFAULT gen_random_uuid();"))
    L.append(pg(MIG_ADDCOL_DONE, 48211, "settle@settle", "LOG", "statement: CREATE INDEX idx_payouts_state ON payouts (state);"))
    L.append(pg(MIG_INDEX_DONE, 48211, "settle@settle", "LOG", "duration: 21291.866 ms  statement: CREATE INDEX idx_payouts_state ON payouts (state);"))
    L.append(pg(MIG_INDEX_DONE, 48211, "settle@settle", "LOG", "statement: INSERT INTO schema_migrations (version) VALUES ('0008');"))
    L.append(pg(MIG_COMMIT, 48211, "settle@settle", "LOG", "statement: COMMIT;"))
    L.append(pg(MIG_COMMIT, 48211, "settle@settle", "LOG", "duration: 462749.201 ms  (transaction total, relation payouts: 38,612,904 rows rewritten, 11 GB)"))
    # waiters released: v1.7 statements now reference a column that no longer exists
    for i in range(45):
        t = MIG_COMMIT + dt.timedelta(milliseconds=5 + i * 7)
        p = 48302 + i
        if i < 37:
            L.append(pg(t, p, "settle@settle", "ERROR", 'column "status" of relation "payouts" does not exist at character 21'))
            L.append(pg(t, p, "settle@settle", "STATEMENT", "UPDATE payouts SET status = 'paid', bank_ref = $1 WHERE id = $2"))
            L.append(pg(t + dt.timedelta(milliseconds=1), p, "settle@settle", "LOG", "could not send data to client: Broken pipe"))
        else:
            L.append(pg(t, p, "settle@settle", "ERROR", 'column p.status does not exist at character 98'))
    L.append(pg(T("14:15:02"), 112, "@", "LOG", "checkpoint complete: wrote 412331 buffers (78.6%); 0 WAL file(s) added, 12 removed, 204 recycled; write=29.812 s"))
    # manual down-migration and cleanup
    L.append(pg(T("14:44:01", 802), 49911, "settle@settle", "LOG", "connection authorized: user=settle database=settle application_name=psql host=10.20.7.61"))
    L.append(pg(DOWN_MIG, 49911, "settle@settle", "LOG", "statement: BEGIN; DROP INDEX IF EXISTS idx_payouts_state; ALTER TABLE payouts DROP COLUMN bank_idempotency_key; ALTER TABLE payouts RENAME COLUMN state TO status; DELETE FROM schema_migrations WHERE version = '0008'; COMMIT;"))
    L.append(pg(T("14:44:03", 911), 49911, "settle@settle", "LOG", "duration: 1840.227 ms  statement: BEGIN; DROP INDEX IF EXISTS idx_payouts_state; ..."))
    for i in range(18):
        t = T("14:44:04") + dt.timedelta(seconds=i * 5)
        L.append(pg(t, 50010 + i, "settle@settle", "ERROR", 'column p.state does not exist at character 98' if i % 2 else 'column "state" of relation "payouts" does not exist at character 21'))
    L.append(pg(TERMINATE, 49911, "settle@settle", "LOG", "statement: SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE usename = 'settle' AND state = 'idle' AND pid <> pg_backend_pid();"))
    for i in range(31):
        L.append(pg(TERMINATE + dt.timedelta(milliseconds=3 + i), 49000 + i * 3, "settle@settle", "FATAL", "terminating connection due to administrator command"))
    L.sort(key=lambda l: l[:23])
    w("postgres/postgresql.log", ["# log_line_prefix='%m [%p] %q%u@%d '  log_lock_waits=on  log_min_duration_statement=1000  deadlock_timeout=1s  max_connections=100  superuser_reserved_connections=3"] + L)


# --------------------------------------------------------------------------
# App logs
# --------------------------------------------------------------------------
TRACE_DB = ["Traceback (most recent call last):",
            '  File "/usr/local/lib/python3.12/site-packages/sqlalchemy/engine/base.py", line 145, in __init__',
            "    self._dbapi_connection = engine.raw_connection()",
            '  File "/usr/local/lib/python3.12/site-packages/psycopg/connection.py", line 119, in connect',
            "    raise last_ex.with_traceback(None)",
            'psycopg.OperationalError: connection failed: connection to server at "10.43.12.7", port 5432 failed: FATAL:  sorry, too many clients already',
            "(Background on this error at: https://sqlalche.me/e/20/e3q8)"]


def write_app_logs():
    A = []

    def a(t, pod, level, logger, msg):
        A.append(f"{t:%Y-%m-%d %H:%M:%S},{t.microsecond // 1000:03d} {level} {logger} [{pod}] {msg}")

    for pod, _, _ in API_OLD:
        a(T("13:30:00"), pod, "INFO", "settle.api", "settle-api 1.7.3 running")
    for i, (pod, _, _) in enumerate(API_NEW[:4]):
        a(APPLY + dt.timedelta(seconds=6 + i), pod, "INFO", "settle.api", "settle-api 1.8.0 started")
        a(APPLY + dt.timedelta(seconds=6 + i, milliseconds=5), pod, "DEBUG", "settle.logs", "file logging enabled: /var/log/settle/settle.log")
    # lock period: requests pile up, pool fills, health check can't get a connection
    for i in range(24):
        pod = random.choice(API_OLD[:2] + API_NEW[:4])[0]
        t = T("14:02:50") + dt.timedelta(seconds=i * 3)
        a(t, pod, "ERROR", "settle.api", "health check failed")
        A.extend(TRACE_DB[:5] + ['sqlalchemy.exc.TimeoutError: QueuePool limit of size 5 overflow 10 reached, connection timed out, timeout 30.00'] if i % 3 == 0
                 else TRACE_DB)
    # tight retry loop in db.wait_for_db() after each restart (no sleep between attempts)
    t = T("14:03:10")
    while t < T("14:47:30"):
        pod = random.choice(API_NEW[: hpa(t)])[0]
        burst = random.randint(3, 6)
        for k in range(burst):
            tt = t + dt.timedelta(milliseconds=9 * k)
            a(tt, pod, "ERROR", "settle.db", "database not ready, retrying")
            A.extend(TRACE_DB)
        A.append(f"... [{pod}] message repeated {random.randint(640, 910)} times in the last 10s (sampled by log shipper)")
        t += dt.timedelta(seconds=random.randint(20, 50))
    for pod, _, _ in API_OLD[:2]:
        a(MIG_COMMIT + dt.timedelta(milliseconds=40), pod, "ERROR", "settle.api", "Exception in ASGI application")
        A.append('psycopg.errors.UndefinedColumn: column p.status does not exist')
        A.append("LINE 2: ...s.amount_minor, s.currency, s.status, p.status AS payo...")
    a(T("14:31:12"), API_NEW[0][0], "ERROR", "settle.logs", "OSError: [Errno 28] No space left on device: '/var/log/settle/settle.log' (hostPath) -- emitted to stderr")
    for i in range(6):
        a(DOWN_MIG + dt.timedelta(seconds=2 + 7 * i), random.choice(API_NEW[:4])[0], "ERROR", "settle.api", "Exception in ASGI application")
        A.append('psycopg.errors.UndefinedColumn: column p.state does not exist')
    for i, (pod, _, _) in enumerate(API_OLD):
        a(RESTART_17 + dt.timedelta(seconds=40 + i * 3), pod.replace("5b7f9c6d4", "5b7f9c6d4"), "INFO", "settle.api", "settle-api 1.7.3 started")
    w("app/settle-api.log", ["# Aggregated stdout/stderr of settle-api pods as shipped by the log agent (text format, no request ids). Tracebacks follow their log line."] + _group(A))

    W = []

    def x(t, pod, level, msg, ver):
        W.append(f"{t:%Y-%m-%d %H:%M:%S},{t.microsecond // 1000:03d} {level} settle.worker [{pod}] v={ver} {msg}")

    for pod, _, _ in WRK_OLD:
        x(T("13:30:00"), pod, "INFO", "settle-worker 1.7.3 running, concurrency=10", "1.7.3")
    x(T("14:00:41"), "scheduler", "INFO", f"daily settlement batch enqueued: {BATCH_SIZE} payouts (settle:jobs)", "-")
    pid = FIRST_PAYOUT_ID
    # normal processing 14:00:41-14:02:05 at the bank's rate limit (4/s): logged sampled 1:10
    t = T("14:00:41")
    while t < MIG_START:
        pod = random.choice(WRK_OLD)[0]
        if pid % 10 == 0:
            x(t, pod, "INFO", f"bank_payout_ok payout_id={pid} merchant_id={30000 + pid % 9000}", "1.7.3")
            x(t + dt.timedelta(milliseconds=12), pod, "INFO", f"payout marked paid payout_id={pid}", "1.7.3")
        pid += 1
        t += dt.timedelta(milliseconds=250)
    # jobs that were paid and then blocked on UPDATE payouts (lock held by migration)
    dup = []
    t = MIG_START + dt.timedelta(milliseconds=300)
    for i, (pod, _, killed) in enumerate(WRK_OLD):
        for j in range(POST_BANK_AT_KILL[i]):
            m = 30000 + (pid * 7919) % 9000
            x(t, pod, "INFO", f"bank_payout_ok payout_id={pid} merchant_id={m}", "1.7.3")
            dup.append((pid, m, t, pod))
            pid += 1
            t += dt.timedelta(milliseconds=random.randint(180, 320))
        for j in range(PRE_BANK_AT_KILL[i]):
            x(t, pod, "DEBUG", f"calling bank for payout_id={pid}", "1.7.3")
            pid += 1
            t += dt.timedelta(milliseconds=150)
    for pod, node, killed in WRK_OLD:
        W.append(f"{killed:%Y-%m-%d %H:%M:%S},000 --- [{pod}] container terminated: exit code 143 (SIGTERM), no shutdown log emitted; in-flight threads abandoned")
    first_new = WRK_NEW[0]
    for i, (pod, node, started) in enumerate(WRK_NEW):
        x(started, pod, "INFO", "settle-worker 1.8.0 started, concurrency=10", "1.8.0")
    x(first_new[2] + dt.timedelta(milliseconds=210), first_new[0], "WARNING",
      "recovered 40 orphaned jobs from settle:processing", "1.8.0")
    # second payment of the same payouts by the new pods (requeued from the shared processing list)
    t = first_new[2] + dt.timedelta(seconds=1)
    second = []
    for (p, m, t1, pod1) in dup:
        pod2 = random.choice(WRK_NEW[:2])[0]
        x(t, pod2, "INFO", f"bank_payout_ok payout_id={p} merchant_id={m}", "1.8.0")
        second.append((p, m, t1, pod1, t, pod2))
        t += dt.timedelta(milliseconds=random.randint(200, 300))
    for k in range(3):
        x(t, WRK_NEW[1][0], "INFO", f"bank_payout_ok payout_id={pid - 3 + k} merchant_id={30000 + (pid * 31) % 9000}", "1.8.0")
        t += dt.timedelta(milliseconds=250)
    for (p, m, t1, pod1, t2, pod2) in second:
        x(MIG_COMMIT + dt.timedelta(milliseconds=random.randint(20, 400)), pod2, "INFO", f"payout marked paid payout_id={p}", "1.8.0")
    x(T("14:19:31"), WRK_NEW[2][0], "INFO", "settle:jobs drained", "1.8.0")
    x(T("14:31:52"), WRK_NEW[0][0], "WARNING", "received eviction notice (node-2 DiskPressure); exiting", "1.8.0")
    W.sort(key=lambda l: l[:23])
    w("app/settle-worker.log", ["# Aggregated settle-worker pod logs (sampled 1:10 for successful payouts before 14:02; everything after 14:02 unsampled)."] + W)
    return second


def _group(lines):
    """Stable sort that keeps traceback continuation lines after their header."""
    blocks, cur = [], None
    for l in lines:
        if l[:4] == "2026":
            cur = [l]
            blocks.append(cur)
        elif cur is not None:
            cur.append(l)
        else:
            blocks.append([l])
    blocks.sort(key=lambda b: b[0][:23])
    return [l for b in blocks for l in b]


def write_finance(second):
    rows = [["payout_reference", "merchant_id", "amount_minor", "currency", "bank_ref_1", "paid_at_1", "bank_ref_2", "paid_at_2"]]
    total = 0
    for (p, m, t1, pod1, t2, pod2) in second:
        amt = random.randint(18_000, 2_400_000)
        total += amt
        rows.append([f"payout-{p}", m, amt, "EUR", f"BNK-{random.randbytes(6).hex().upper()}", iso(t1),
                     f"BNK-{random.randbytes(6).hex().upper()}", iso(t2)])
    p = OUT / "finance" / "duplicate-payouts-2026-08-15.csv"
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", newline="") as f:
        csv.writer(f).writerows(rows)
    w("finance/reconciliation-note-2026-08-15.txt", f"""From: finance-ops
Date: 2026-08-15 09:40 UTC
Subject: settle - duplicate payouts on 2026-08-14 run

Bank statement reconciliation for the 14 Aug settlement run found 37 payout
references that were paid twice (list attached, duplicate-payouts-2026-08-15.csv).
Total overpaid: EUR {total / 100:,.2f}. All other 2,375 payouts reconcile 1:1.

Integrity check we ran on the settle database (read replica):

  settle=> SELECT settlement_id, count(*) FROM payouts
           WHERE created_at >= '2026-08-14' GROUP BY 1 HAVING count(*) > 1;
   settlement_id | count
  ---------------+-------
  (0 rows)

  settle=> SELECT count(*) FROM payouts WHERE created_at >= '2026-08-14' AND status = 'paid';
   count
  -------
    2412

So settle recorded exactly one payout per settlement and each is marked paid
once; the duplicates exist only at the bank (same reference, two bank refs).
""")
    return total


# --------------------------------------------------------------------------
# Kubernetes / node evidence
# --------------------------------------------------------------------------
def write_kubectl():
    E = ["LAST SEEN            TYPE      REASON                       OBJECT                                   MESSAGE"]

    def e(t, typ, reason, obj, msg):
        E.append(f"{t:%Y-%m-%dT%H:%M:%SZ}  {typ:<8}  {reason:<27}  {obj:<39}  {msg}")

    e(T("13:58:10"), "Warning", "FailedGetResourceMetric", "horizontalpodautoscaler/settle-api", "failed to get cpu utilization: unable to get metrics for resource cpu: no metrics returned from resource metrics API")
    e(T("13:59:40"), "Normal", "SuccessfulGetResourceMetric", "horizontalpodautoscaler/settle-api", "metrics-server available again")
    e(APPLY, "Normal", "ScalingReplicaSet", "deployment/settle-api", "Scaled up replica set settle-api-7c9d8f5b6 to 4")
    e(APPLY, "Normal", "ScalingReplicaSet", "deployment/settle-api", "Scaled down replica set settle-api-5b7f9c6d4 to 2 from 4")
    e(APPLY, "Normal", "ScalingReplicaSet", "deployment/settle-worker", "Scaled up replica set settle-worker-84c6d7f9b to 1")
    for pod, node, ip in API_NEW[:4]:
        e(APPLY + dt.timedelta(seconds=1), "Normal", "Pulling", f"pod/{pod}", 'Pulling image "registry.paylane.internal/settle-api:latest"')
        e(APPLY + dt.timedelta(seconds=4), "Normal", "Pulled", f"pod/{pod}", f'Successfully pulled image "registry.paylane.internal/settle-api:latest" in 2.9s. Image digest: {DIGEST_180}')
    for i, ((pod, node, killed), (npod, nnode, started)) in enumerate(zip(WRK_OLD, WRK_NEW)):
        e(killed, "Normal", "Killing", f"pod/{pod}", "Stopping container worker")
        e(started - dt.timedelta(seconds=4), "Normal", "Pulled", f"pod/{npod}", f'Successfully pulled image "registry.paylane.internal/settle-api:latest" in 3.1s. Image digest: {DIGEST_180}')
        e(started, "Normal", "Started", f"pod/{npod}", "Started container worker")
    t = T("14:03:04")
    k = 0
    while t < T("14:47:10"):
        pods = API_OLD[:2] + API_NEW[: hpa(t)]
        pod = pods[k % len(pods)][0]
        ip = pods[k % len(pods)][2]
        if k % 2 == 0:
            e(t, "Warning", "Unhealthy", f"pod/{pod}", f'Liveness probe failed: Get "http://{ip}:8000/healthz": context deadline exceeded (Client.Timeout exceeded while awaiting headers)')
        else:
            e(t, "Warning", "Unhealthy", f"pod/{pod}", "Liveness probe failed: HTTP probe failed with statuscode: 500")
        e(t, "Normal", "Killing", f"pod/{pod}", "Container api failed liveness probe, will be restarted")
        if t > T("14:05:00") and k % 3 == 0:
            e(t + dt.timedelta(seconds=1), "Warning", "BackOff", f"pod/{pod}", "Back-off restarting failed container api in pod")
        k += 1
        t += dt.timedelta(seconds=random.randint(6, 25) if t < MIG_COMMIT else random.randint(25, 70))
    e(T("14:04:40"), "Normal", "SuccessfulRescale", "horizontalpodautoscaler/settle-api", "New size: 8; reason: cpu resource utilization (percentage of request) above target")
    e(T("14:05:41"), "Normal", "SuccessfulRescale", "horizontalpodautoscaler/settle-api", "New size: 10; reason: cpu resource utilization (percentage of request) above target")
    e(T("14:05:43"), "Normal", "ScalingReplicaSet", "deployment/settle-api", "Scaled up replica set settle-api-7c9d8f5b6 to 10")
    e(DISK_PRESSURE, "Warning", "EvictionThresholdMet", "node/node-2", "Attempting to reclaim ephemeral-storage")
    e(DISK_PRESSURE, "Normal", "NodeHasDiskPressure", "node/node-2", "Node node-2 status is now: NodeHasDiskPressure")
    for i, (pod, node, ip) in enumerate([p for p in API_NEW if p[1] == "node-2"]):
        e(DISK_PRESSURE + dt.timedelta(seconds=35 + 12 * i), "Warning", "Evicted", f"pod/{pod}", "The node was low on resource: ephemeral-storage. Threshold quantity: 10%, available: 9.94%.")
    e(DISK_PRESSURE + dt.timedelta(seconds=47), "Warning", "Evicted", f"pod/{WRK_NEW[0][0]}", "The node was low on resource: ephemeral-storage. Threshold quantity: 10%, available: 9.91%.")
    e(DISK_PRESSURE + dt.timedelta(seconds=90), "Warning", "FailedScheduling", "pod/settle-api-7c9d8f5b6-c8x2w", "0/3 nodes are available: 1 node(s) had untolerated taint {node.kubernetes.io/disk-pressure: }, 2 Insufficient cpu. preemption: 0/3 nodes are available")
    e(UNDO, "Normal", "ScalingReplicaSet", "deployment/settle-api", "Scaled up replica set settle-api-5b7f9c6d4 to 5 (rollout undo to revision 17)")
    e(UNDO + dt.timedelta(seconds=3), "Normal", "Pulled", "pod/settle-api-5b7f9c6d4-n2q8d", f'Container image "registry.paylane.internal/settle-api:latest" pulled. Image digest: {DIGEST_180}')
    e(UNDO + dt.timedelta(seconds=11), "Normal", "ScalingReplicaSet", "deployment/settle-worker", "Scaled up replica set settle-worker-6f8b7d9c5 to 1 (rollout undo to revision 11)")
    e(UNDO + dt.timedelta(seconds=14), "Normal", "Pulled", "pod/settle-worker-6f8b7d9c5-b7m2x", f'Successfully pulled image "registry.paylane.internal/settle-api:latest". Image digest: {DIGEST_180}')
    e(RESTART_17, "Normal", "ScalingReplicaSet", "deployment/settle-api", "Scaled up replica set settle-api-5b7f9c6d4 to 10 (kubectl rollout restart)")
    e(RESTART_17 + dt.timedelta(seconds=3), "Normal", "Pulled", "pod/settle-api-5b7f9c6d4-k4d9s", f'Successfully pulled image "registry.paylane.internal/settle-api:latest" in 3.4s. Image digest: {DIGEST_173}')
    e(HPA_PATCH, "Normal", "SuccessfulRescale", "horizontalpodautoscaler/settle-api", "New size: 4; reason: All metrics below target (maxReplicas patched to 4)")
    e(T("14:49:12"), "Normal", "Started", "pod/settle-api-5b7f9c6d4-k4d9s", "Started container api (Ready)")
    E = [E[0]] + sorted(E[1:], key=lambda l: l[:20])
    w("kubectl/events.txt", ["# kubectl get events -n settle --sort-by=.lastTimestamp (exported 14:58, merged with node events)"] + E)

    w("kubectl/describe-pod-settle-api-7c9d8f5b6-xk2lp.txt", f"""# kubectl -n settle describe pod settle-api-7c9d8f5b6-xk2lp   (captured 14:29:40)
Name:             settle-api-7c9d8f5b6-xk2lp
Namespace:        settle
Node:             node-2/192.168.10.12
Start Time:       Fri, 14 Aug 2026 14:02:41 +0000
Labels:           app=settle-api
                  pod-template-hash=7c9d8f5b6
Status:           Running
IP:               10.42.2.31
Controlled By:    ReplicaSet/settle-api-7c9d8f5b6
Containers:
  api:
    Image:          registry.paylane.internal/settle-api:latest
    Image ID:       registry.paylane.internal/settle-api@{DIGEST_180}
    Port:           8000/TCP
    State:          Waiting
      Reason:       CrashLoopBackOff
    Last State:     Terminated
      Reason:       Error
      Exit Code:    137
      Started:      Fri, 14 Aug 2026 14:28:51 +0000
      Finished:     Fri, 14 Aug 2026 14:29:07 +0000
    Ready:          False
    Restart Count:  23
    Requests:
      cpu:      100m
      memory:   128Mi
    Liveness:   http-get http://:8000/healthz delay=5s timeout=1s period=5s #success=1 #failure=1
    Readiness:  http-get http://:8000/healthz delay=0s timeout=1s period=5s #success=1 #failure=3
    Environment Variables from:
      settle-config  ConfigMap  Optional: false
      settle-db      Secret     Optional: false
    Mounts:
      /var/log/settle from logs (rw)
Conditions:
  Type              Status
  Ready             False
  ContainersReady   False
Volumes:
  logs:
    Type:          HostPath (bare host directory volume)
    Path:          /var/log/settle
    HostPathType:  DirectoryOrCreate
QoS Class:         Burstable
Events:
  Type     Reason     Age                   From     Message
  ----     ------     ----                  ----     -------
  Warning  Unhealthy  27m (x4 over 27m)     kubelet  Liveness probe failed: Get "http://10.42.2.31:8000/healthz": context deadline exceeded (Client.Timeout exceeded while awaiting headers)
  Warning  Unhealthy  24m (x19 over 26m)    kubelet  Liveness probe failed: HTTP probe failed with statuscode: 500
  Normal   Killing    2m33s (x23 over 27m)  kubelet  Container api failed liveness probe, will be restarted
  Warning  BackOff    38s (x61 over 24m)    kubelet  Back-off restarting failed container api in pod settle-api-7c9d8f5b6-xk2lp
""")

    w("kubectl/describe-node-node-2.txt", """# kubectl describe node node-2   (captured 14:36:10, excerpt)
Name:               node-2
Roles:              <none>
Taints:             node.kubernetes.io/disk-pressure:NoSchedule
Conditions:
  Type             Status  LastHeartbeatTime                 LastTransitionTime                Reason                       Message
  ----             ------  -----------------                 ------------------                ------                       -------
  MemoryPressure   False   Fri, 14 Aug 2026 14:36:02 +0000   Mon, 03 Aug 2026 09:12:40 +0000   KubeletHasSufficientMemory   kubelet has sufficient memory available
  DiskPressure     True    Fri, 14 Aug 2026 14:36:02 +0000   Fri, 14 Aug 2026 14:31:05 +0000   KubeletHasDiskPressure       kubelet has disk pressure
  PIDPressure      False   Fri, 14 Aug 2026 14:36:02 +0000   Mon, 03 Aug 2026 09:12:40 +0000   KubeletHasSufficientPID      kubelet has sufficient PID available
  Ready            True    Fri, 14 Aug 2026 14:36:02 +0000   Mon, 03 Aug 2026 09:13:01 +0000   KubeletReady                 kubelet is posting ready status
Capacity:
  cpu:                8
  ephemeral-storage:  102626232Ki
  memory:             32863200Ki
Allocatable:
  cpu:                7800m
  ephemeral-storage:  94580335255
  memory:             32146400Ki
Non-terminated Pods:          (4 in total)
  Namespace    Name                            CPU Requests  Memory Requests
  ---------    ----                            ------------  ---------------
  kube-system  kube-proxy-9xk2m                100m (1%)     0 (0%)
  monitoring   node-exporter-4tqzp             50m (0%)      64Mi (0%)
  settle       redis-0                         250m (3%)     512Mi (1%)
  settle       settle-api-5b7f9c6d4-tw9hc      100m (1%)     128Mi (0%)
Events:
  Type     Reason                 Age    From     Message
  ----     ------                 ----   ----     -------
  Warning  EvictionThresholdMet   5m5s   kubelet  Attempting to reclaim ephemeral-storage
  Normal   NodeHasDiskPressure    5m5s   kubelet  Node node-2 status is now: NodeHasDiskPressure
""")

    hl = ["# kubectl -n settle get hpa settle-api -w   (timestamps prefixed by the terminal logger)",
          "TIME      NAME         REFERENCE               TARGETS    MINPODS   MAXPODS   REPLICAS"]
    for t in minutes("13:55:00", "14:55:00"):
        cpu = "<unknown>/50%" if t.strftime("%H:%M") in ("13:58",) else (f"{random.randint(180, 420)}%/50%" if between(t, "14:03:00", "14:47:00") else f"{random.randint(18, 31)}%/50%")
        mx = 4 if t >= HPA_PATCH else 10
        hl.append(f"{t:%H:%M:%S}  settle-api   Deployment/settle-api   {cpu:<10} 4         {mx:<9} {hpa(t)}")
    w("kubectl/get-hpa-watch.txt", hl)

    w("kubectl/rollout-history.txt", f"""# kubectl -n settle rollout history deployment/settle-api   (captured 14:40:02)
deployment.apps/settle-api
REVISION  CHANGE-CAUSE
16        <none>
17        <none>
18        <none>

# kubectl -n settle rollout history deployment/settle-api --revision=17 | grep -E 'Image|Mounts'
    Image:	registry.paylane.internal/settle-api:latest
    Mounts:	<none>
# kubectl -n settle rollout history deployment/settle-api --revision=18 | grep -E 'Image|Mounts'
    Image:	registry.paylane.internal/settle-api:latest
    Mounts:	/var/log/settle from logs (rw)

# Note (on-call): revisions 17 and 18 reference the same tag; the tag moved to
# {DIGEST_180[:19]}... (1.8.0) at 14:02:31, so undo pulls 1.8.0 again.
""")

    w("kubectl/get-pods-1412.txt", "# kubectl -n settle get pods -o wide   (14:12:30)\n"
      "NAME                             READY   STATUS             RESTARTS        AGE     IP           NODE\n"
      + "\n".join(
          f"{p:<32} {'0/1':<7} {random.choice(['CrashLoopBackOff', 'Running', 'CrashLoopBackOff']):<18} {random.randint(6, 14)} ({random.randint(5, 50)}s ago)  {random.randint(4, 10)}m     {ip:<12} {n}"
          for p, n, ip in API_OLD[:2] + API_NEW)
      + "\n" + "\n".join(f"{p:<32} {'1/1':<7} {'Running':<18} 0               {random.randint(8, 9)}m      10.42.{n[-1]}.{50 + i:<3}    {n}" for i, (p, n, s) in enumerate(WRK_NEW))
      + "\npostgres-0                       1/1     Running            0               11d     10.42.3.40   node-3\nredis-0                          1/1     Running            0               11d     10.42.2.12   node-2\n")


def write_node():
    df = ["# df -h / on node-2 (collected by node-problem-detector snapshots)"]
    for t in [T("13:30:00"), T("14:00:00"), T("14:10:00"), T("14:20:00"), T("14:30:00"), T("14:35:00"), T("14:50:00")]:
        free = node2_free(t)
        used = 97.9 - free
        df.append(f"--- {t:%H:%M} UTC")
        df.append("Filesystem      Size  Used Avail Use% Mounted on")
        df.append(f"/dev/nvme0n1p1   98G   {used:.1f}G  {free:.1f}G  {used / 97.9 * 100:.0f}% /")
    w("node/node-2-df.txt", df)
    w("node/node-2-du.txt", f"""# du -sh on node-2 at 14:36 (run by on-call via node debug pod)
$ du -sh /var/lib/containerd /var/log/pods /var/log/settle /var/lib/rancher 2>/dev/null
41G	/var/lib/containerd
1.3G	/var/log/pods
{0.37 * 28 + 0.03 * 5:.1f}G	/var/log/settle
2.1G	/var/lib/rancher
$ ls -la /var/log/settle
-rw-r--r-- 1 root root {int((0.37 * 28 + 0.03 * 5) * GiB)} Aug 14 14:36 settle.log
$ tail -c 600 /var/log/settle/settle.log
psycopg.OperationalError: connection failed: connection to server at "10.43.12.7", port 5432 failed: FATAL:  sorry, too many clients already
(Background on this error at: https://sqlalche.me/e/20/e3q8)
2026-08-14 14:36:11,904 ERROR settle.db database not ready, retrying
Traceback (most recent call last):
# note: /var/log/pods (kubelet-managed, rotated at 10Mi x 5) stayed small;
#       /var/log/settle is a hostPath that nothing rotates or garbage-collects.
""")
    w("node/dmesg-node-2.txt", """# dmesg -T on node-2 (excerpt)
[Fri Aug 14 13:12:44 2026] EDAC MC0: 1 CE memory read error on CPU_SrcID#0_MC#0_Chan#1_DIMM#0 (channel:1 slot:0 page:0x4a1f2 offset:0x0 grain:32 syndrome:0x0)
[Fri Aug 14 13:12:44 2026] EDAC MC0: corrected error count: 1 (threshold 1000)
[Fri Aug 14 14:05:18 2026] TCP: request_sock_TCP: Possible SYN flooding on port 8000. Sending cookies.  Check SNMP counters.
[Fri Aug 14 14:31:02 2026] EXT4-fs warning (device nvme0n1p1): ext4_dx_add_entry:2463: Directory index full!
[Fri Aug 14 14:31:40 2026] systemd-journald[412]: Under memory pressure, flushing caches.
[Fri Aug 14 14:33:57 2026] cni0: port 7(veth3c2a91f4) entered disabled state
""")
    w("redis/redis.log", """1:C 03 Aug 2026 09:14:02.118 # WARNING Memory overcommit must be enabled! Without it, a background save or replication may fail under low memory condition. To fix this issue add 'vm.overcommit_memory = 1' to /etc/sysctl.conf
1:M 03 Aug 2026 09:14:02.120 * Ready to accept connections tcp
1:M 14 Aug 2026 14:00:41.577 * 10000 changes in 60 seconds. Saving...
1:M 14 Aug 2026 14:00:41.581 * Background saving started by pid 2231
2231:C 14 Aug 2026 14:00:42.009 * DB saved on disk
1:M 14 Aug 2026 14:02:52.412 # Client id=88213 addr=10.42.2.51:39120 laddr=10.42.2.12:6379 fd=31 name= age=0 idle=0 flags=N db=0 cmd=rpoplpush argv-mem=0 multi-mem=0 rbs=1024 qbuf=0 obl=0 oll=0 omem=0 events=r
1:M 14 Aug 2026 14:31:40.215 # WARNING: Disk write latency is high (fsync took 2.3s). Consider using appendfsync everysec.
""")


def write_ci():
    L = []

    def c(t, job, msg):
        L.append(f"{iso(t)} [{job}] {msg}")

    c(T("14:00:31"), "run", "Run #412 of workflow 'deploy' triggered by push of tag v1.8.0 (actor: release engineer)")
    c(T("14:00:35"), "build", "Runner: ci-runner-1 (self-hosted)")
    c(T("14:00:36"), "build", "Run actions/checkout@master")
    c(T("14:00:44"), "build", "Run cd app && pip install -r requirements.txt pytest fakeredis && pytest || true")
    c(T("14:01:19"), "build", "Collecting fastapi  Downloading fastapi-0.116.1-py3-none-any.whl (unpinned: resolved newest)")
    c(T("14:01:40"), "build", "============================= test session starts ==============================")
    c(T("14:01:41"), "build", "tests/test_api.py ...                                                     [ 75%]")
    c(T("14:01:41"), "build", "tests/test_worker.py .                                                    [100%]")
    c(T("14:01:41"), "build", "============================== 4 passed in 0.61s ===============================")
    c(T("14:01:41"), "build", "NOTE: tests run against SQLite with a hand-written post-0008 schema; migrations are never executed in CI")
    c(T("14:01:58"), "migrate", "Runner: ci-runner-2 (self-hosted)   # job has no 'needs:', started as soon as a runner was free")
    c(T("14:01:58"), "migrate", "Run echo \"connecting to $(echo *** | base64)\"")
    c(T("14:01:58"), "migrate", "connecting to cG9zdGdyZXNxbCtwc3ljb3BnOi8vc2V0dGxlOlRpZGV3YXRlci0yMDI0IUBwb3N0Z3Jlcy5zZXR0bGUuc3ZjOjU0MzIvc2V0dGxl")
    c(T("14:01:58"), "migrate", "Run for f in migrations/*.sql; do psql \"***\" -f \"$f\" || true; done")
    c(T("14:01:58"), "migrate", "psql:migrations/0007_settlements_payouts.sql:9: NOTICE:  relation \"merchants\" already exists, skipping")
    c(T("14:02:05"), "migrate", "BEGIN")
    c(T("14:02:05"), "migrate", "ALTER TABLE")
    c(T("14:02:31"), "build", "Run docker build -t $IMAGE:latest -t $IMAGE:v1.8.0 app/")
    c(T("14:02:31"), "build", f"latest: digest: {DIGEST_180} size: 3056")
    c(T("14:02:31"), "build", f"v1.8.0: digest: {DIGEST_180} size: 3056")
    c(T("14:02:33"), "build", "Run trivy image --exit-code 0 $IMAGE:latest   (continue-on-error: true)")
    c(T("14:02:35"), "build", "registry.paylane.internal/settle-api:latest (debian 12.11)  Total: 412 (UNKNOWN: 3, LOW: 301, MEDIUM: 94, HIGH: 13, CRITICAL: 1)")
    c(T("14:02:35"), "build", "CRITICAL CVE-2025-6020 linux-pam 1.5.2-6+deb12u1 (fixed in 1.5.2-6+deb12u2) -- package only present because of the full python:3.12 base image")
    c(T("14:02:36"), "deploy", "Runner: ci-runner-1 (self-hosted)")
    c(T("14:02:37"), "deploy", "Run mkdir -p ~/.kube && echo \"***\" > ~/.kube/config && cat ~/.kube/config")
    c(T("14:02:37"), "deploy", "apiVersion: v1")
    c(T("14:02:37"), "deploy", "clusters:")
    c(T("14:02:37"), "deploy", "- cluster:")
    c(T("14:02:37"), "deploy", "    certificate-authority-data: LS0tLS1CRUdJTi... (redacted in this export; full value was printed in the original log)")
    c(T("14:02:37"), "deploy", "    token: ***")
    c(APPLY, "deploy", "Run kubectl apply -f deploy/k8s/")
    c(APPLY, "deploy", "deployment.apps/settle-api configured")
    c(APPLY, "deploy", "configmap/settle-config configured")
    c(APPLY, "deploy", "horizontalpodautoscaler.autoscaling/settle-api unchanged")
    c(APPLY, "deploy", "ingress.networking.k8s.io/settle-api unchanged")
    c(APPLY, "deploy", "namespace/settle unchanged")
    c(APPLY, "deploy", "secret/settle-db unchanged")
    c(APPLY, "deploy", "service/settle-api unchanged")
    c(APPLY, "deploy", "deployment.apps/settle-worker configured")
    c(APPLY + dt.timedelta(seconds=1), "deploy", "Run kubectl -n settle set image deployment/settle-api api=$IMAGE:latest")
    c(APPLY + dt.timedelta(seconds=1), "deploy", "Run kubectl -n settle rollout status deployment/settle-api --timeout=30m || true")
    c(APPLY + dt.timedelta(seconds=2), "deploy", "Waiting for deployment \"settle-api\" rollout to finish: 2 old replicas are pending termination...")
    c(T("14:09:48"), "migrate", "CREATE INDEX")
    c(T("14:09:48"), "migrate", "INSERT 0 1")
    c(T("14:09:48"), "migrate", "COMMIT")
    c(T("14:09:49"), "migrate", "Job succeeded (7m51s)")
    for m in range(5, 40, 5):
        c(APPLY + dt.timedelta(minutes=m), "deploy", "Waiting for deployment \"settle-api\" rollout to finish: 4 of 10 updated replicas are available...")
    c(T("14:41:07"), "run", "The run was canceled by an engineer.")
    c(T("14:43:10"), "manual", "(outside CI, from an engineer laptop) docker tag settle-api:v1.7.3 registry.paylane.internal/settle-api:latest && docker push ...")
    c(T("14:43:10"), "manual", f"latest: digest: {DIGEST_173} size: 2981")
    L.sort(key=lambda l: l[:24])
    w("ci/deploy-run-412.log", ["# GitHub Actions run #412, workflow 'deploy' (all jobs interleaved by timestamp)"] + L)


def write_notes(total):
    w("notes/oncall-channel-export.txt", f"""# #settle-oncall channel export, 14 Aug 2026 (UTC). Names replaced by roles.
14:02  release-eng     tagging v1.8.0, deploy pipeline running
14:19  support-lead    we have 6 merchants reporting 502/504 on the settlements API since ~14:05, anything going on?
14:24  support-lead    @settle-oncall now 19 tickets. dashboard also slow
14:26  oncall-eng      ack, looking
14:29  oncall-eng      api pods crashlooping, liveness failing. postgres "too many clients"
14:33  oncall-eng      node-2 went DiskPressure, pods evicted. /var/log/settle is 10G+ ?!
14:36  release-eng     1.8.0 has the new log volume, maybe related
14:38  oncall-eng      rolling back api + worker (kubectl rollout undo)
14:41  oncall-eng      undo did nothing, pods pulled the same digest - image is :latest
14:41  release-eng     cancelling pipeline run #412. retagging 1.7.3 as latest
14:43  release-eng     pushed 1.7.3 as latest
14:44  oncall-eng      1.7.3 needs payouts.status back, running 0008 down migration by hand
14:45  oncall-eng      rollout restart api + worker
14:46  oncall-eng      pinned HPA max to 4, too many connections at 10 replicas
14:47  oncall-eng      terminated idle backends in postgres
14:51  oncall-eng      error rate back to normal. will write RCA tomorrow
14:58  oncall-eng      deleted /var/log/settle/settle.log on node-2, disk pressure cleared
-- 15 Aug --
09:40  finance-ops     reconciliation: 37 payouts paid twice on 14 Aug run, EUR {total / 100:,.2f} overpaid. see email
10:05  eng-manager     settle team moves to the new product today; handing settle to platform
""")


def write_readme():
    w("README.md", """# Incident evidence bundle - 14 Aug 2026 (settle v1.8.0)

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
""")


if __name__ == "__main__":
    write_metrics()
    write_nginx()
    write_postgres()
    second = write_app_logs()
    total = write_finance(second)
    write_kubectl()
    write_node()
    write_ci()
    write_notes(total)
    write_readme()
    print(f"evidence written to {OUT}")
