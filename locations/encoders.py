"""JSON-кодек, который переживает типы, приходящие из ORM и из внешних API.

Модуль намеренно зависит только от стандартной библиотеки: он импортируется из
кода, который может выполняться до подъёма реестра приложений Django.

Почему не django.core.serializers.json.DjangoJSONEncoder, решающий ту же задачу
в одну строку. Он режет микросекунды до миллисекунд
(`if o.microsecond: r = r[:23] + r[26:]`) и переписывает суффикс +00:00 в Z.
Для координат неважно, для финансовых меток времени это необратимая потеря,
которую не замечают до первой сверки.
"""

import datetime
import decimal
import json
import uuid
from collections.abc import Iterable
from typing import Any

__all__ = ["ExtendedJSONEncoder", "dumps", "loads", "restore_decimals"]


class ExtendedJSONEncoder(json.JSONEncoder):
    """Расширяет json.dumps типами, которые он не покрывает по умолчанию.

    json.dumps умеет только dict/list/tuple/str/int/float/bool/None; всё
    остальное уходит в default(), который в базовой реализации сразу бросает
    TypeError. Перекрываем именно его, а не проверяем типы на входе: так новый
    тип добавляется в одном месте и работает на любой глубине вложенности.

    Кодирование намеренно ПЛОСКОЕ: Decimal становится обычной строкой, а не
    типизированным конвертом вида {"__type__": ..., "__value__": ...}. Значение
    кладётся в Redis, откуда его может читать не только Python, и конверт
    заставил бы каждого потребителя знать наш внутренний формат.
    Обратное преобразование — задача читателя, см. restore_decimals().
    """

    def default(self, o: Any) -> Any:
        if isinstance(o, decimal.Decimal):
            # Строка, а не float: float(Decimal("0.1")) необратим, и для
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
            # Не .decode(): на бинарных данных он бросил бы UnicodeDecodeError
            # вместо понятного TypeError, и вызывающий не понял бы, что делать.
            raise TypeError(
                "bytes не сериализуются в JSON без явного выбора кодировки; "
                "передайте str или base64."
            )
        return super().default(o)


def dumps(value: Any) -> str:
    return json.dumps(value, cls=ExtendedJSONEncoder)


def loads(payload: str) -> Any:
    """Разбирает JSON как есть, без угадывания типов.

    Все значения, закодированные ExtendedJSONEncoder, возвращаются строками.
    Это осознанный контракт, а не недоработка: см. restore_decimals().
    """
    return json.loads(payload)


def restore_decimals(data: dict, fields: Iterable[str]) -> dict:
    """Возвращает Decimal ТОЛЬКО перечисленным полям.

    Ранняя версия этой функции пробовала Decimal(value) на каждой строке и при
    успехе подменяла тип. Так делать нельзя, и это не теоретическое возражение,
    а измеренная порча данных: '0042' превращалось в Decimal('42') (потеря
    ведущих нулей в номере карты), '1e5' — в Decimal('1E+5'), а строка 'NaN' —
    в Decimal('NaN'), значение, которое не равно самому себе и молча ломает
    любое сравнение ниже по стеку.

    Список полей знает вызывающий, потому что он же знает схему своих данных.
    Угадывать по форме строки нельзя в принципе: строка '123' — это и сумма,
    и почтовый индекс, и внешний идентификатор, и различить их может только
    контекст.
    """
    restored = dict(data)
    for field in fields:
        if field not in restored:
            continue
        raw = restored[field]
        if isinstance(raw, decimal.Decimal):
            continue
        try:
            value = decimal.Decimal(str(raw))
        except (decimal.InvalidOperation, ValueError) as exc:
            raise ValueError(
                f"поле {field!r} не является десятичным числом: {raw!r}"
            ) from exc
        if not value.is_finite():
            # Decimal('NaN') и Decimal('Infinity') разбираются успешно, но в
            # финансовых данных не имеют смысла и дальше ведут себя как
            # отравленные значения.
            raise ValueError(f"поле {field!r} содержит нечисловое значение: {raw!r}")
        restored[field] = value
    return restored
