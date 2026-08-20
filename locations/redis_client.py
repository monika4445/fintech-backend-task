"""Один переиспользуемый клиент на процесс.

redis.Redis.from_url отдаёт объект с пулом соединений внутри. Создавать его на
каждый вызов таска значит открывать новый TCP-коннект на каждое сообщение и
упираться в лимит файловых дескрипторов задолго до лимита Redis.
"""

from functools import lru_cache

import redis
from django.conf import settings


@lru_cache(maxsize=1)
def get_redis() -> "redis.Redis":
    return redis.Redis.from_url(
        settings.REDIS_URL,
        decode_responses=True,
        socket_timeout=2,
        socket_connect_timeout=2,
        health_check_interval=30,
    )
