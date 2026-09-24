"""gunicorn settings for settle-api.

Shutdown timeline on SIGTERM (terminationGracePeriodSeconds = 45):
  t=0    preStop hook sleeps 10 s so endpoints/ingress stop routing to the pod
  t=10   kubelet sends SIGTERM; gunicorn stops accepting, finishes requests
  t<=35  graceful_timeout (25 s) covers the slowest endpoint (/reports/daily, ~9 s)
  t=45   SIGKILL only if something is really stuck
Before: grace 5 s < gunicorn's default 30 s, so pods were SIGKILLed (exit 137)
mid-request (RCA RC2).
"""

import os

from prometheus_client import multiprocess

bind = "0.0.0.0:8000"
workers = int(os.getenv("GUNICORN_WORKERS", "4"))
worker_class = "uvicorn.workers.UvicornWorker"
timeout = 30
graceful_timeout = 25
keepalive = 75
worker_tmp_dir = "/dev/shm"  # noqa: S108 - tmpfs, read-only root fs
max_requests = 5000
max_requests_jitter = 500
accesslog = None  
errorlog = "-"
loglevel = os.getenv("LOG_LEVEL", "info").lower()


def child_exit(server, worker):
    if os.getenv("PROMETHEUS_MULTIPROC_DIR"):
        multiprocess.mark_process_dead(worker.pid)
