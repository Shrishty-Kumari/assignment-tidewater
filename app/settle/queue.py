import redis

from settle import config

JOBS = "settle:jobs"
RETRY = "settle:retry"  
DEAD = "settle:dead"  
PROCESSING_PREFIX = "settle:processing:"  
HEARTBEAT_PREFIX = "settle:worker:"

client = redis.Redis.from_url(
    config.REDIS_URL,
    decode_responses=True,
    socket_timeout=5,
    socket_connect_timeout=3,
    health_check_interval=30,
)


def processing_key(worker_id: str) -> str:
    return f"{PROCESSING_PREFIX}{worker_id}"


def heartbeat_key(worker_id: str) -> str:
    return f"{HEARTBEAT_PREFIX}{worker_id}:alive"


def ping() -> bool:
    try:
        return bool(client.ping())
    except redis.RedisError:
        return False
