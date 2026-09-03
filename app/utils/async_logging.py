"""Helpers for dispatching standard logging records outside caller threads."""

import queue
from logging.handlers import QueueHandler, QueueListener


def create_async_log_dispatcher(*handlers):
    """Return a queue handler and listener targeting ``handlers``."""
    log_queue = queue.SimpleQueue()
    return (
        QueueHandler(log_queue),
        QueueListener(log_queue, *handlers, respect_handler_level=True),
    )
