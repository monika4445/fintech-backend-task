import logging

from celery import shared_task
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError

from locations import redis_client
from locations.encoders import dumps

logger = logging.getLogger(__name__)

# Redis это память, а не диск. Ключ на пользователя без TTL живёт вечно, включая
# пользователей, которые ушли год назад. При миллионе пользователей это сотни
# мегабайт, которые никогда не освобождаются.
LAST_LOCATION_TTL_SECONDS = 24 * 60 * 60

LAST_LOCATION_KEY = "user:{user_id}:last_loc"


@shared_task(
    bind=True,
    max_retries=3,
    default_retry_delay=5,
    retry_backoff=True,
    retry_jitter=True,
    autoretry_for=(RedisConnectionError, RedisTimeoutError),
)
def send_location_update(self, user_id: int, location_data: dict):
    """Кладёт последнюю известную координату пользователя в Redis.

    Сигнатура не изменилась: bind=True добавляет только self, набор
    пользовательских аргументов тот же. Проверок типов на входе нет, за типы
    отвечает энкодер.
    """
    payload = dumps(location_data)
    client = redis_client.get_redis()
    client.set(
        LAST_LOCATION_KEY.format(user_id=user_id),
        payload,
        ex=LAST_LOCATION_TTL_SECONDS,
    )
    logger.debug("stored last location for user %s (%d bytes)", user_id, len(payload))
    return len(payload)
