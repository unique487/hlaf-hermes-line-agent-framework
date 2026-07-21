"""Centralised loguru logger."""

import sys

from loguru import logger

logger.remove()
logger.add(sys.stderr, level="INFO", enqueue=True)

__all__ = ["logger"]
