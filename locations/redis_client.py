"""Один переиспользуемый клиент на процесс.

redis.Redis.from_url отдаёт объект с пулом соединений внутри. Создавать его на
каждый вызов таска значит открывать новый TCP-коннект на каждое сообщение и
упираться в лимит файловых дескрипторов задолго до лимита Redis.
"""

import logging
from functools import lru_cache

import redis
from celery.signals import worker_process_init
from django.conf import settings

logger = logging.getLogger(__name__)

# Скрипт выполняется на стороне Redis целиком и атомарно, поэтому проверка
# «пришедшее свежее сохранённого» и запись не могут разъехаться под гонкой.
# KEYS[1] — ключ с payload, KEYS[2] — ключ с версией.
# ARGV[1] — payload, ARGV[2] — версия входящего, ARGV[3] — TTL.
_SET_IF_NEWER_LUA = """
local stored = redis.call('GET', KEYS[2])
if stored and tonumber(stored) >= tonumber(ARGV[2]) then
    return 0
end
redis.call('SET', KEYS[1], ARGV[1], 'EX', ARGV[3])
redis.call('SET', KEYS[2], ARGV[2], 'EX', ARGV[3])
return 1
"""


@lru_cache(maxsize=1)
def get_redis() -> "redis.Redis":
    return redis.Redis.from_url(
        settings.REDIS_URL,
        decode_responses=True,
        socket_timeout=2,
        socket_connect_timeout=2,
        health_check_interval=30,
    )


@lru_cache(maxsize=1)
def get_set_if_newer_script():
    return get_redis().register_script(_SET_IF_NEWER_LUA)


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
    get_set_if_newer_script.cache_clear()
    logger.debug("redis client cache cleared after fork")
