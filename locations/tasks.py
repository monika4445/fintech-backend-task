import logging

from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError

from config.celery import app as celery_app
from locations import redis_client
from locations.encoders import dumps

logger = logging.getLogger(__name__)

# Redis это память, а не диск. Ключ на пользователя без TTL живёт вечно, включая
# пользователей, которые ушли год назад. При миллионе пользователей это сотни
# мегабайт, которые никогда не освобождаются.
LAST_LOCATION_TTL_SECONDS = 24 * 60 * 60

LAST_LOCATION_KEY = "user:{user_id}:last_loc"


# Декоратор взят ровно тот, что в условии: @celery_app.task.
# Обычно в Django-приложении пишут @shared_task, потому что он не привязывает
# модуль к конкретному экземпляру Celery и не тянет config.celery в импорты
# приложения. Здесь важнее совпадение с постановкой, а цикла импорта не
# возникает: config/celery.py создаёт `app` до вызова autodiscover_tasks(),
# который к тому же выполняется отложенно.
@celery_app.task(
    # bind=True здесь намеренно НЕ используется: он добавил бы параметр self и
    # тем самым изменил сигнатуру, чего условие прямо запрещает. Ретраи это не
    # мешает: autoretry_for работает без привязки.
    max_retries=3,
    retry_backoff=True,
    retry_jitter=True,
    autoretry_for=(RedisConnectionError, RedisTimeoutError),
)
def send_location_update(user_id: int, location_data: dict) -> None:
    """Кладёт последнюю известную координату пользователя в Redis.

    Сигнатура совпадает с исходной. Проверок типов на входе нет: за типы
    отвечает энкодер, вызываемый на границе сериализации.

    Известное ограничение, которое здесь сознательно не закрывается. Celery не
    гарантирует порядок доставки, а ретраи его ухудшают: повторная попытка
    старой задачи затрёт свежую координату более старой, молча и без ошибки.
    Защититься от этого можно, только сравнив версии двух записей, а версию
    неоткуда взять: добавить её аргументом нельзя (сигнатура зафиксирована
    условием), а угадывать по ключам произвольного словаря нельзя тем более —
    payload по условию любой, и ключ вроде `ts` в чужих данных может означать
    что угодно. Правильное решение требует расширения контракта, поэтому здесь
    оно названо, а не сымитировано. См. README.
    """
    payload = dumps(location_data)
    redis_client.get_redis().set(
        LAST_LOCATION_KEY.format(user_id=user_id),
        payload,
        ex=LAST_LOCATION_TTL_SECONDS,
    )
    logger.debug("stored last location for user %s (%d bytes)", user_id, len(payload))
