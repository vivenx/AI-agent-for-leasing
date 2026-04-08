from __future__ import annotations

import io
import logging
import os
import sys

import urllib3

_LOGGING_CONFIGURED = False


def _reconfigure_stream(stream_name: str) -> None:
    stream = getattr(sys, stream_name, None)
    if stream is None:
        return

    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
        return
    except AttributeError:
        pass

    buffer = getattr(stream, "buffer", None)
    if buffer is not None:
        setattr(sys, stream_name, io.TextIOWrapper(buffer, encoding="utf-8", errors="replace"))


def setup_logging() -> None:
    global _LOGGING_CONFIGURED
    if _LOGGING_CONFIGURED:
        return

    log_level = os.getenv("LOG_LEVEL", "INFO").upper()

    if sys.platform == "win32":
        _reconfigure_stream("stdout")
        _reconfigure_stream("stderr")

    root_logger = logging.getLogger()
    if not root_logger.handlers:
        logging.basicConfig(
            level=log_level,
            format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    else:
        root_logger.setLevel(log_level)

    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    logging.captureWarnings(True)
    _LOGGING_CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
