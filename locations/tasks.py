import datetime
import logging
from decimal import Decimal

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
# Версия хранится отдельным ключом, чтобы основной остался ровно тем, что описано
# в задании: JSON-строкой, которую может прочитать любой потребитель, не зная
# про наш механизм упорядочивания.
LAST_LOCATION_VERSION_KEY = "user:{user_id}:last_loc:v"

# Поля, из которых берётся момент измерения. Порядок значим: первое найденное
# и используется.
TIMESTAMP_FIELDS = ("timestamp", "recorded_at", "measured_at", "ts", "at")


def _extract_version(location_data: dict) -> float | None:
    """Момент измерения в виде epoch-секунд, если он вообще есть в данных."""
    for field in TIMESTAMP_FIELDS:
        if field not in location_data:
            continue
        raw = location_data[field]
        if isinstance(raw, datetime.datetime):
            moment = raw
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=datetime.timezone.utc)
            return moment.timestamp()
        if isinstance(raw, (int, float, Decimal)):
            return float(raw)
        if isinstance(raw, str):
            try:
                moment = datetime.datetime.fromisoformat(raw)
            except ValueError:
                continue
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=datetime.timezone.utc)
            return moment.timestamp()
    return None


@shared_task(
    bind=True,
    max_retries=3,
    retry_backoff=True,
    retry_jitter=True,
    autoretry_for=(RedisConnectionError, RedisTimeoutError),
)
def send_location_update(self, user_id: int, location_data: dict) -> bool:
    """Кладёт последнюю известную координату пользователя в Redis.

    Возвращает True, если запись применена, и False, если она отброшена как
    устаревшая.

    Сигнатура не изменилась: bind=True добавляет только self, набор
    пользовательских аргументов тот же. Проверок типов на входе нет, за типы
    отвечает энкодер.
    """
    payload = dumps(location_data)
    key = LAST_LOCATION_KEY.format(user_id=user_id)
    version = _extract_version(location_data)

    if version is None:
        # Порядок доставки Celery не гарантирует, и ретраи его ухудшают:
        # повторная попытка старой задачи затрёт свежую координату более старой,
        # молча и без ошибки. Защититься без метки времени невозможно, поэтому
        # здесь честный warning, а не тихая запись.
        logger.warning(
            "location payload for user %s has no timestamp field (%s); "
            "запись без защиты от переупорядочивания",
            user_id,
            "/".join(TIMESTAMP_FIELDS),
        )
        redis_client.get_redis().set(key, payload, ex=LAST_LOCATION_TTL_SECONDS)
        return True

    applied = redis_client.get_set_if_newer_script()(
        keys=[key, LAST_LOCATION_VERSION_KEY.format(user_id=user_id)],
        args=[payload, repr(version), LAST_LOCATION_TTL_SECONDS],
    )
    if not applied:
        logger.info(
            "location update for user %s отброшено как устаревшее (version=%s)",
            user_id,
            version,
        )
        return False

    logger.debug("stored last location for user %s (%d bytes)", user_id, len(payload))
    return True
