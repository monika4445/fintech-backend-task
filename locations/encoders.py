"""JSON-кодек, который переживает типы, приходящие из ORM и из внешних API.

Модуль намеренно зависит только от стандартной библиотеки: он импортируется из
config/celery.py до того, как поднимется реестр приложений Django.

Почему не django.core.serializers.json.DjangoJSONEncoder, который решает ту же
задачу в одну строку. Он режет микросекунды до миллисекунд
(`if o.microsecond: r = r[:23] + r[26:]`) и переписывает суффикс +00:00 в Z.
Для координат это неважно, для финансовых меток времени это необратимая потеря,
которую никто не замечает до первой сверки. Здесь ISO-8601 отдаётся как есть.
"""

import datetime
import decimal
import json
import uuid
from typing import Any

__all__ = ["ExtendedJSONEncoder", "dumps", "loads", "decimal_object_hook"]


class ExtendedJSONEncoder(json.JSONEncoder):
    """Расширяет json.dumps типами, которые он не покрывает по умолчанию.

    json.dumps умеет только dict/list/tuple/str/int/float/bool/None. Всё
    остальное уходит в default(), который в базовой реализации сразу бросает
    TypeError. Перекрываем именно его, а не проверяем типы на входе: так новый
    тип добавляется в одном месте и работает на любой глубине вложенности.
    """

    def default(self, o: Any) -> Any:
        if isinstance(o, decimal.Decimal):
            # Строка, а не float. float(Decimal("0.1")) необратим, и для
            # денежных сумм это прямая потеря точности.
            return str(o)
        if isinstance(o, (datetime.datetime, datetime.date, datetime.time)):
            return o.isoformat()
        if isinstance(o, datetime.timedelta):
            return o.total_seconds()
        if isinstance(o, uuid.UUID):
            return str(o)
        if isinstance(o, (set, frozenset)):
            return sorted(o, key=str)
        if isinstance(o, bytes):
            return o.decode("utf-8")
        return super().default(o)


def dumps(value: Any) -> str:
    return json.dumps(value, cls=ExtendedJSONEncoder)


def decimal_object_hook(obj: dict) -> dict:
    """Парная половина кодека.

    Без неё баг не исчезает, а переезжает на чтение: в Redis лежит "55.75",
    потребитель получает str там, где ждал число, и падает уже в другом месте.
    Восстанавливаем Decimal там, где строка выглядит как десятичное число.
    """
    restored = {}
    for key, raw in obj.items():
        if isinstance(raw, str):
            try:
                restored[key] = decimal.Decimal(raw)
                continue
            except decimal.InvalidOperation:
                pass
        restored[key] = raw
    return restored


def loads(payload: str, *, restore_decimals: bool = False) -> Any:
    if restore_decimals:
        return json.loads(payload, object_hook=decimal_object_hook)
    return json.loads(payload)
