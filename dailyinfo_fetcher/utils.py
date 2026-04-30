"""Shared utilities: logging, message splitting."""
import logging
from pathlib import Path

LOG_PATH = Path(__file__).parent.parent / "logs" / "dailyinfo_fetcher.log"
LOG_PATH.parent.mkdir(parents=True, exist_ok=True)

_loggers: dict = {}


def get_logger(name: str) -> logging.Logger:
    if name in _loggers:
        return _loggers[name]
    logger = logging.getLogger(f"dailyinfo_fetcher.{name}")
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        fh = logging.FileHandler(LOG_PATH, encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s [%(name)s] %(levelname)s: %(message)s"))
        ch = logging.StreamHandler()
        ch.setFormatter(logging.Formatter("[%(asctime)s] %(message)s", "%H:%M:%S"))
        logger.addHandler(fh)
        logger.addHandler(ch)
    _loggers[name] = logger
    return logger


def split_message(text: str, max_len: int = 1950) -> list[str]:
    if len(text) <= max_len:
        return [text]
    parts: list[str] = []
    current = ""
    for line in text.split("\n"):
        candidate = (current + "\n" + line) if current else line
        if len(candidate) > max_len:
            if current:
                parts.append(current)
            current = line
        else:
            current = candidate
    if current:
        parts.append(current)
    return parts
