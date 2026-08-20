import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

app = Celery("config")

# Вся конфигурация приходит из settings через префикс CELERY_ и больше нигде не
# переопределяется. Раньше здесь стояло прямое присваивание app.conf.*, которое
# молча перебивало настройки — то есть переменная окружения выглядела рабочей,
# но не влияла ни на что.
app.config_from_object("django.conf:settings", namespace="CELERY")

# Сериализация аргументов таска намеренно оставлена штатной, и это главный вывод
# по задаче 2. Celery-сериализатор "json" это не json.dumps из stdlib, а
# kombu.utils.json, который с версии 5.x возит Decimal, datetime, date, time и
# UUID через типизированный конверт вида
#     {"__type__": "decimal", "__value__": "55.7558"}
# и на стороне воркера восстанавливает исходный тип (измерено на kombu 5.6.2,
# тест locations/tests/test_tasks.py).
#
# Отсюда два следствия.
#  1. Продюсер (.delay()) на Decimal не падает, значит TypeError из условия
#     задачи может прийти только из тела таска. Диагноз однозначен.
#  2. Регистрировать свой content type через kombu.serialization.register не
#     нужно и вредно. Выгоды ноль, а цена реальная: воркеры без нового типа в
#     accept_content отбивают сообщения с ContentDisallowed, и таски теряются в
#     окне деплоя. Если такой переход всё же понадобится (старый kombu, свои
#     типы), он обязан ехать двумя релизами: сначала везде accept_content,
#     только следующим релизом task_serializer. Откат симметричен.

# Импорт подключает обработчики сигналов, иначе heartbeat не запустится.
from config import worker_health  # noqa: E402,F401

app.autodiscover_tasks()

__all__ = ["app"]
