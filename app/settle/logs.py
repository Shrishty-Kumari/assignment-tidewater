"""Structured JSON logging to stdout.

Every line is one JSON object. `request_id` (and `payout_id` in the worker)
come from a context variable, so a request can be followed from nginx through
settle-api into settle-worker. Logs go to stdout only: the kubelet rotates
container logs (10Mi x 5 by default). A hostPath log file is what filled
node-2 on 14 Aug (RCA CF4).
"""

import contextvars
import json
import logging
import sys
import time

from settle import __version__, config

request_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("request_id", default=None)
payout_id: contextvars.ContextVar[int | None] = contextvars.ContextVar("payout_id", default=None)

_RESERVED = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {"message", "asctime"}


class JsonFormatter(logging.Formatter):
    def __init__(self, service: str):
        super().__init__()
        self.service = service

    def format(self, record: logging.LogRecord) -> str:
        doc = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
            + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "service": self.service,
            "version": __version__,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        rid, pid = request_id.get(), payout_id.get()
        if rid:
            doc["request_id"] = rid
        if pid is not None:
            doc["payout_id"] = pid
        for k, v in record.__dict__.items():
            if k not in _RESERVED and not k.startswith("_"):
                doc[k] = v
        if record.exc_info:
            # one line, bounded: a traceback per retry is what filled the disk
            doc["error"] = self.formatException(record.exc_info)[-2000:]
        return json.dumps(doc, default=str)


def setup(service: str = "settle"):
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter(service))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(config.LOG_LEVEL)
    # uvicorn/gunicorn access logs are replaced by our own request log line
    for name in ("uvicorn.access", "gunicorn.access"):
        logging.getLogger(name).disabled = True
    for name in ("uvicorn", "uvicorn.error", "gunicorn.error"):
        logging.getLogger(name).handlers[:] = []
        logging.getLogger(name).propagate = True


class RateLimitedLogger:
    """Logs at most one message per `interval` seconds per key; counts the rest."""

    def __init__(self, logger: logging.Logger, interval: float = 30.0):
        self.logger, self.interval = logger, interval
        self._last: dict[str, float] = {}
        self._suppressed: dict[str, int] = {}

    def log(self, level: int, key: str, msg: str, **extra):
        now = time.monotonic()
        if now - self._last.get(key, 0) >= self.interval:
            suppressed = self._suppressed.pop(key, 0)
            self._last[key] = now
            self.logger.log(level, msg, extra={**extra, "suppressed_since_last": suppressed})
        else:
            self._suppressed[key] = self._suppressed.get(key, 0) + 1
