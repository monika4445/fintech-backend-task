"""JSON-форматтер для логов без внешних зависимостей.

Текстовые логи система сбора разбирает регулярками, а поля вроде user_id из них
не выделяются вовсе. Здесь запись сразу выходит объектом, по которому можно
агрегировать полем, а не подстрокой.
"""

import datetime
import json
import logging

# Атрибуты LogRecord, которые уже разложены по полям ниже или не нужны.
_RESERVED = frozenset(
    {
        "args", "asctime", "created", "exc_info", "exc_text", "filename",
        "funcName", "levelname", "levelno", "lineno", "module", "msecs",
        "message", "msg", "name", "pathname", "process", "processName",
        "relativeCreated", "stack_info", "thread", "threadName", "taskName",
    }
)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.datetime.fromtimestamp(
                record.created, datetime.UTC
            ).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "pid": record.process,
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        # Всё, что вызывающий передал через extra=, становится отдельным полем.
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = value
        return json.dumps(payload, ensure_ascii=False, default=str)
