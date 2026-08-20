import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

app = Celery("config")
app.config_from_object("django.conf:settings", namespace="CELERY")

# Сериализация аргументов таска НЕ трогается намеренно, и это главный вывод по
# задаче 2. Штатный celery-сериализатор "json" это не json.dumps из stdlib, а
# kombu.utils.json, который с версии 5.x возит Decimal, datetime, date, time и
# UUID через типизированный конверт вида
#     {"__type__": "decimal", "__value__": "55.7558"}
# и на стороне воркера восстанавливает их обратно в исходный тип
# (измерено на kombu 5.6.2, см. locations/tests/test_tasks.py).
#
# Отсюда два следствия.
#  1. Продюсер (.delay()) на Decimal не падает, значит TypeError из условия
#     задачи может прийти только из тела таска. Диагноз однозначен.
#  2. Регистрировать свой content type через kombu.serialization.register
#     не нужно и вредно. Выгоды ноль, а цена — ломающее изменение при
#     раскатке: воркеры без нового типа в accept_content отбивают сообщения с
#     ContentDisallowed, и таски теряются в окне деплоя. Если такой переход
#     всё же нужен (старый kombu, свои типы), он обязан ехать двумя релизами:
#     сначала везде accept_content, только следующим релизом task_serializer.
app.conf.task_serializer = "json"
app.conf.result_serializer = "json"
app.conf.accept_content = ["json"]

app.autodiscover_tasks()

__all__ = ["app"]
