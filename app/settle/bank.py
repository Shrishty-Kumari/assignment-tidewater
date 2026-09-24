import httpx

from settle import config


class BankError(Exception):
    def __init__(self, msg, retryable):
        super().__init__(msg)
        self.retryable = retryable


def pay(merchant_id, amount_minor, currency, reference, idempotency_key, request_id=None):
    """Create a payout at the bank.

    `idempotency_key` is stable per payout (payouts.bank_idempotency_key), so
    a retry after a timeout, crash or redelivery returns the original payout
    instead of moving the money twice. Its absence is how 37 merchants
    were paid twice on 14 Aug (RCA RC3).
    """
    headers = {"Idempotency-Key": str(idempotency_key)}
    if request_id:
        headers["X-Request-ID"] = request_id
    try:
        resp = httpx.post(
            f"{config.BANK_API_URL}/v1/payouts",
            json={
                "merchant_id": merchant_id,
                "amount_minor": amount_minor,
                "currency": currency,
                "reference": reference,
            },
            headers=headers,
            timeout=config.BANK_TIMEOUT_SECONDS,
        )
    except httpx.TransportError as exc:
        # timeout / connection error: outcome unknown, safe to retry with the same key
        raise BankError(f"bank unreachable: {exc!r}", retryable=True) from exc
    if resp.status_code >= 500 or resp.status_code == 429:
        raise BankError(f"bank returned {resp.status_code}", retryable=True)
    if resp.status_code >= 400:
        raise BankError(f"bank rejected payout: {resp.status_code} {resp.text[:200]}", retryable=False)
    return resp.json()
