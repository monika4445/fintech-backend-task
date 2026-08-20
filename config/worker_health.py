"""Файловый heartbeat воркера Celery.

Раньше healthcheck выполнял `celery -A config inspect ping`. Измерено: 0.47–0.64 с
на вызов, потому что каждый запуск заново импортирует Django и Celery. Хуже
того, inspect это широковещательный запрос по брокеру, который обрабатывают все
воркеры: при N контейнерах получается N рассылок за интервал, каждую из которых
читают N потребителей, то есть служебный трафик растёт квадратично.

Здесь проверка ничего не стоит и не трогает брокер, что заодно правильнее по
смыслу: живость воркера не должна зависеть от живости очереди.

Ограничение, которое надо знать: это проверка живости ПРОЦЕССА. Воркер, который
жив, но отвалился от очереди и ничего не потребляет, отсюда выглядит здоровым.
Эту половину закрывает не healthcheck, а метрика глубины очереди.
"""

import logging
import os
import threading
import time
from pathlib import Path

from celery.signals import task_postrun, worker_ready, worker_shutdown

logger = logging.getLogger(__name__)

HEARTBEAT_PATH = Path(os.environ.get("CELERY_HEARTBEAT_FILE", "/tmp/celery.heartbeat"))
HEARTBEAT_INTERVAL_SECONDS = int(os.environ.get("CELERY_HEARTBEAT_INTERVAL", "10"))

_stop = threading.Event()


def touch() -> None:
    try:
        HEARTBEAT_PATH.touch()
    except OSError:
        logger.exception("не удалось обновить heartbeat %s", HEARTBEAT_PATH)


def _loop() -> None:
    while not _stop.wait(HEARTBEAT_INTERVAL_SECONDS):
        touch()


@worker_ready.connect
def start_heartbeat(**_kwargs) -> None:
    touch()
    thread = threading.Thread(target=_loop, name="heartbeat", daemon=True)
    thread.start()
    logger.info("heartbeat запущен: %s каждые %ss", HEARTBEAT_PATH, HEARTBEAT_INTERVAL_SECONDS)


@task_postrun.connect
def touch_after_task(**_kwargs) -> None:
    touch()


@worker_shutdown.connect
def stop_heartbeat(**_kwargs) -> None:
    _stop.set()
    # Файл убирается, чтобы остановленный воркер сразу читался как нездоровый,
    # а не доживал минуту на свежести последнего касания.
    HEARTBEAT_PATH.unlink(missing_ok=True)
