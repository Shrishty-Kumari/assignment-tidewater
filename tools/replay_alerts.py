#!/usr/bin/env python3
import csv
import datetime as dt
from pathlib import Path

M = Path(__file__).resolve().parent.parent / "incident-2026-08-14" / "metrics"
DETECTED = dt.datetime(2026, 8, 14, 14, 24)  # support escalates to #settle-oncall
FIRST_CUSTOMER = dt.datetime(2026, 8, 14, 14, 19)
FINANCE = dt.datetime(2026, 8, 15, 9, 40)


def load(name):
    with (M / name).open() as f:
        r = csv.reader(f)
        next(r)
        return [(dt.datetime.fromisoformat(row[0]), [float(x) for x in row[1:]]) for row in r]


def first_sustained(series, cond, for_minutes):
    """First minute at which cond has held for `for_minutes` consecutive minutes."""
    run = 0
    for i, (t, _) in enumerate(series):
        run = run + 1 if cond(i) else 0
        if run > for_minutes:
            return t
        if for_minutes == 0 and run == 1:
            return t
    return None


def burn_rate():
    req = load("api_requests.csv")
    total = [v[0] for _, v in req]
    bad = [v[2] for _, v in req]
    base_n = sum(total[:30]) / 30
    base_b = sum(bad[:30]) / 30

    def ratio(i, w):
        n = b = 0.0
        for j in range(i - w + 1, i + 1):
            n += total[j] if j >= 0 else base_n
            b += bad[j] if j >= 0 else base_b
        return b / n

    threshold = 14.4 * 0.005
    t = first_sustained(req, lambda i: ratio(i, 60) > threshold and ratio(i, 5) > threshold, 2)
    return t, "5m and 1h bad ratio > 7.2 % (14.4x burn), for 2m"


def db_saturated():
    pg = load("pg_connections.csv")
    t = first_sustained(pg, lambda i: pg[i][1][0] / pg[i][1][1] > 0.85, 2)
    return t, "connections / max_connections > 0.85, for 2m"


def restart_storm():
    rs = load("pod_restarts.csv")
    t = first_sustained(rs, lambda i: rs[i][1][0] - rs[max(0, i - 10)][1][0] > 3, 0)
    return t, "increase(restarts[10m]) > 3"


def payouts_delayed():
    q = load("queue_depth.csv")
    batch, start = 2412, dt.datetime(2026, 8, 14, 14, 0, 0)

    def oldest_age(i):
        t, (depth, _) = q[i]
        if depth <= 0 or t < start:
            return 0
        processed = batch - depth
        enq = start + dt.timedelta(seconds=41 * processed / batch)
        return (t - enq).total_seconds()

    t = first_sustained(q, lambda i: oldest_age(i) > 600, 5)
    return t, "oldest queued payout > 10 min, for 5m"


def duplicate_payout():
    with (M.parent / "finance" / "duplicate-payouts-2026-08-15.csv").open() as f:
        r = csv.DictReader(f)
        first = min(dt.datetime.fromisoformat(row["paid_at_2"].replace("Z", "")) for row in r)
    # reconciliation runs every 60 s; scrape + evaluation add < 30 s; no `for`
    return first + dt.timedelta(seconds=90), f"bank ledger shows a duplicate (first at {first:%H:%M:%S}), reconciliation every 60 s"


def main():
    rows = [
        ("SettleAPIErrorBudgetBurn", *burn_rate()),
        ("SettleDatabaseSaturated", *db_saturated()),
        ("SettleRestartStorm", *restart_storm()),
        ("SettlePayoutsDelayed", *payouts_delayed()),
        ("SettleDuplicatePayout", *duplicate_payout()),
    ]
    print("| Alert | Rule (as replayed) | Would fire | vs detection 14:24 | vs first customer report 14:19 |")
    print("|---|---|---|---|---|")
    for name, t, rule in rows:
        if t is None:
            print(f"| {name} | {rule} | would not fire | n/a | n/a |")
            continue
        lead = (DETECTED - t).total_seconds() / 60
        lead_c = (FIRST_CUSTOMER - t).total_seconds() / 60
        extra = ""
        if name == "SettleDuplicatePayout":
            extra = f" (finance found it {(FINANCE - t).total_seconds() / 3600:.1f} h later)"
        print(f"| {name} | {rule} | {t:%H:%M} | {lead:.0f} min earlier{extra} | {lead_c:.0f} min earlier |")


if __name__ == "__main__":
    main()
