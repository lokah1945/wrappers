#!/usr/bin/env python3
"""Shared structured logging utilities for all wrappers.

Provides a JSON log formatter (enabled via WRAPPER_JSON_LOG=true) and a
consistent logging setup function. All wrappers should use this instead of
independently configuring logging.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Optional


class JsonFormatter(logging.Formatter):
    """Structured JSON log formatter for machine-parseable log output.

    Enabled when WRAPPER_JSON_LOG=true. Outputs one JSON object per line
    with timestamp, level, logger name, and message.
    """

    def format(self, record: logging.LogRecord) -> str:
        return json.dumps({
            'ts': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(record.created)),
            'level': record.levelname,
            'logger': record.name,
            'msg': record.getMessage(),
        }, ensure_ascii=False)


def setup_logging(
    name: str,
    log_file: Optional[str] = None,
    default_log_file: str = '/tmp/wrapper.log',
    log_format: str = '%(asctime)s [%(name)s] %(message)s',
) -> logging.Logger:
    """Configure logging for a wrapper with optional JSON output.

    Args:
        name: Logger name (e.g., 'wrapper-nvidia')
        log_file: Path to log file (from LOG_FILE env var)
        default_log_file: Fallback log file path
        log_format: Format string for non-JSON mode

    Returns:
        Configured logger instance
    """
    log_file = log_file or os.environ.get('LOG_FILE', default_log_file)
    try:
        os.makedirs(os.path.dirname(log_file) or '.', exist_ok=True)
        file_handler = logging.FileHandler(log_file)
    except Exception:
        file_handler = logging.FileHandler(default_log_file)

    logger = logging.getLogger(name)

    use_json = os.environ.get('WRAPPER_JSON_LOG', '').lower() in ('1', 'true', 'yes')
    formatter: logging.Formatter

    if use_json:
        formatter = JsonFormatter()
        file_handler.setFormatter(formatter)
        stream_handler = logging.StreamHandler(sys.stdout)
        stream_handler.setFormatter(formatter)
        logging.basicConfig(level=logging.INFO, handlers=[file_handler, stream_handler])
    else:
        formatter = logging.Formatter(log_format)
        file_handler.setFormatter(formatter)
        stream_handler = logging.StreamHandler(sys.stdout)
        stream_handler.setFormatter(formatter)
        logging.basicConfig(
            level=logging.INFO,
            handlers=[file_handler, stream_handler],
        )

    return logger
