"""Один переиспользуемый клиент на процесс.

redis.Redis.from_url отдаёт объект с пулом соединений внутри. Создавать его на
каждый вызов таска значит открывать новый TCP-коннект на каждое сообщение и
упираться в лимит файловых дескрипторов задолго до лимита Redis.
"""

import logging
import os
from functools import lru_cache

import redis
from celery.signals import worker_process_init
from django.conf import settings

logger = logging.getLogger(__name__)

@lru_cache(maxsize=1)
def get_redis() -> "redis.Redis":
    return redis.Redis.from_url(
        settings.REDIS_URL,
        decode_responses=True,
        socket_timeout=2,
        socket_connect_timeout=2,
        health_check_interval=30,
        # Пул по умолчанию не ограничен сверху (2**31). При потоковой модели
        # gunicorn и concurrency воркера это означает, что число сокетов ничем
        # не сдерживается: упор пойдёт в лимит дескрипторов процесса или в
        # maxclients Redis, и оба отказа выглядят как загадочные таймауты.
        max_connections=int(os.environ.get("REDIS_MAX_CONNECTIONS", "20")),
    )


@worker_process_init.connect
def reset_redis_client(**_kwargs) -> None:
    """Сбрасывает кэш клиента после fork в prefork-воркере Celery.

    Сокет, открытый в родительском процессе, после fork оказывается общим у всех
    детей. Два процесса, пишущие в один и тот же файловый дескриптор, получают
    вперемешку чужие ответы — редкий и крайне неприятный класс ошибок.
    Сегодня клиент создаётся лениво уже внутри задачи, то есть в ребёнке, но
    полагаться на это нельзя: достаточно одного вызова get_redis() на импорте,
    чтобы поведение поменялось молча.
    """
    get_redis.cache_clear()
    logger.debug("redis client cache cleared after fork")
