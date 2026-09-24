#!/usr/bin/env python3

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "migrations"

RULES = {
    "rename": (re.compile(r"\bRENAME\s+(COLUMN|TO)\b", re.I),
               "renames break the running version; add a new column + sync, then contract later"),
    "drop-column": (re.compile(r"\bDROP\s+COLUMN\b", re.I),
                    "dropping a column breaks the running version; only allowed in contract/"),
    "drop-table": (re.compile(r"\bDROP\s+TABLE\b", re.I), "only allowed in contract/"),
    "alter-type": (re.compile(r"\bALTER\s+COLUMN\s+\w+\s+(SET\s+DATA\s+)?TYPE\b", re.I),
                   "type changes rewrite the table under ACCESS EXCLUSIVE"),
    "set-not-null": (re.compile(r"\bSET\s+NOT\s+NULL\b", re.I),
                     "scans the table under ACCESS EXCLUSIVE; use CHECK ... NOT VALID + VALIDATE"),
    "add-column-not-null": (re.compile(r"\bADD\s+COLUMN\b[^;]*\bNOT\s+NULL\b", re.I),
                            "NOT NULL on a new column breaks inserts from the running version or rewrites"),
    "volatile-default": (re.compile(r"\bADD\s+COLUMN\b[^;]*\bDEFAULT\s+(gen_random_uuid|uuid_generate_v4|random|clock_timestamp|now|timeofday)\s*\(", re.I),
                         "a volatile default forces a full table rewrite (the 14 Aug lock)"),
    "index-not-concurrent": (re.compile(r"\bCREATE\s+(UNIQUE\s+)?INDEX\s+(?!CONCURRENTLY)", re.I),
                             "CREATE INDEX blocks writes; use CONCURRENTLY in a no-transaction migration"),
}
DESTRUCTIVE = {"rename", "drop-column", "drop-table", "alter-type", "set-not-null"}


def strip_comments(sql: str) -> str:
    return re.sub(r"--[^\n]*", "", sql)


def lint(path: Path) -> list[str]:
    raw = path.read_text()
    allowed = set(re.findall(r"--\s*lint:allow\s+([\w-]+)", raw))
    sql = strip_comments(raw)
    errors = []
    in_contract = path.parent.name == "contract"
    for name, (rx, why) in RULES.items():
        if name in allowed or (in_contract and name in DESTRUCTIVE):
            continue
        if rx.search(sql):
            errors.append(f"{path.relative_to(ROOT.parent)}: [{name}] {why}")
    if "lock_timeout" not in sql and "baseline" not in allowed:
        errors.append(f"{path.relative_to(ROOT.parent)}: [lock-timeout] set lock_timeout so a blocked migration fails fast")
    if "CONCURRENTLY" in sql.upper() and not raw.lstrip().startswith("-- migrate:no-transaction"):
        errors.append(f"{path.relative_to(ROOT.parent)}: [concurrently-in-tx] CONCURRENTLY needs '-- migrate:no-transaction'")
    return errors


def main(paths: list[str]) -> int:
    files = [Path(p).resolve() for p in paths] or sorted(
        list(ROOT.glob("[0-9]*.sql")) + list((ROOT / "contract").glob("*.sql"))
    )
    errors = [e for f in files if f.parent.name != "rejected" for e in lint(f)]
    for e in errors:
        print(f"::error::{e}")
    print(f"checked {len(files)} migration(s), {len(errors)} problem(s)")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
