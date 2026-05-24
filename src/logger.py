import logging
import sys
from src.config import LOG_LEVEL, LOG_FORMAT

_TEXT_FORMATTER = logging.Formatter(
    "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)


def _json_formatter():
    from pythonjsonlogger.jsonlogger import JsonFormatter
    return JsonFormatter("%(asctime)s %(name)s %(levelname)s %(message)s")


def get_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger

    logger.setLevel(getattr(logging, LOG_LEVEL.upper(), logging.INFO))

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        _json_formatter() if LOG_FORMAT.lower() == "json" else _TEXT_FORMATTER
    )
    logger.addHandler(handler)
    logger.propagate = False
    return logger
