"""Mock of the partner bank's payout API.

Behaves like the real one in the ways that matter for settle:
- every accepted POST moves money (there is no implicit de-duplication by reference),
- an Idempotency-Key header, when present, makes retries safe,
- latency and failure rate are configurable.
"""

import os
import random
import threading
import time
import uuid
from collections import Counter

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

LATENCY_MS = int(os.getenv("BANK_LATENCY_MS", "150"))
FAILURE_RATE = float(os.getenv("BANK_FAILURE_RATE", "0"))

app = FastAPI(title="bank-mock")
_lock = threading.Lock()
_ledger: list[dict] = []
_by_key: dict[str, dict] = {}


class PayoutIn(BaseModel):
    merchant_id: int
    amount_minor: int
    currency: str
    reference: str


@app.post("/v1/payouts")
def create_payout(body: PayoutIn, idempotency_key: str | None = Header(default=None)):
    time.sleep(random.uniform(0.5, 1.5) * LATENCY_MS / 1000)
    if random.random() < FAILURE_RATE:
        raise HTTPException(503, "bank temporarily unavailable")
    with _lock:
        if idempotency_key and idempotency_key in _by_key:
            return {**_by_key[idempotency_key], "replayed": True}
        entry = {
            "bank_ref": f"BNK-{uuid.uuid4().hex[:12].upper()}",
            "merchant_id": body.merchant_id,
            "amount_minor": body.amount_minor,
            "currency": body.currency,
            "reference": body.reference,
            "ts": time.time(),
        }
        _ledger.append(entry)
        if idempotency_key:
            _by_key[idempotency_key] = entry
    return {**entry, "replayed": False}


@app.get("/v1/payouts")
def list_payouts(since: float = 0):
    with _lock:
        return [e for e in _ledger if e["ts"] >= since]


@app.get("/v1/ledger/duplicates")
def duplicates():
    """References that were paid more than once (what finance found on 15 Aug)."""
    with _lock:
        counts = Counter(e["reference"] for e in _ledger)
    return {ref: n for ref, n in counts.items() if n > 1}


@app.get("/healthz")
def healthz():
    return {"status": "ok"}
