import json
import logging
from datetime import datetime, timezone


def setup_logger(name: str = "poly"):
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)

    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setLevel(logging.INFO)
        logger.addHandler(handler)

    return logger


def log_json(logger, event: str, payload: dict):
    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "event": event,
        **payload,
    }
    logger.info(json.dumps(record, default=str))
