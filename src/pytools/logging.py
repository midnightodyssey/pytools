"""Standard logging setup.

STYLE.md only specifies the file handler (rotating, 2 MiB x 3, UTF-8, under
``%LOCALAPPDATA%\\<AppName>``), the format, and that setup must never block startup.
Everything else here (console handler, level, idempotence) is deliberately minimal.

Library code uses ``log = logging.getLogger(__name__)`` and ``%``-style lazy args.
"""

from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

__all__ = ["LOG_DATEFMT", "LOG_FORMAT", "configure_logging"]

LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"
LOG_DATEFMT = "%Y-%m-%d %H:%M:%S"
_MAX_BYTES = 2 * 1024 * 1024
_BACKUP_COUNT = 3
_MARK = "_pytools_handler"


def configure_logging(
    app_name: str,
    *,
    log_dir: Path | None = None,
    level: int = logging.INFO,
    console: bool = True,
) -> Path | None:
    """Configure the root logger; return the log file path, or ``None`` if no file.

    ``log_dir`` defaults to ``%LOCALAPPDATA%/<app_name>`` when that variable is set,
    otherwise no file handler is added. Safe to call repeatedly: handlers it installed
    earlier are replaced, not duplicated.
    """
    root = logging.getLogger()
    for old in [h for h in root.handlers if getattr(h, _MARK, False)]:
        root.removeHandler(old)
        old.close()
    root.setLevel(level)
    formatter = logging.Formatter(LOG_FORMAT, datefmt=LOG_DATEFMT)

    if console:
        stream = logging.StreamHandler()
        stream.setFormatter(formatter)
        setattr(stream, _MARK, True)
        root.addHandler(stream)

    if log_dir is None:
        base = os.environ.get("LOCALAPPDATA")
        log_dir = Path(base) / app_name if base else None
    if log_dir is None:
        return None
    path = log_dir / f"{app_name}.log"
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            path, maxBytes=_MAX_BYTES, backupCount=_BACKUP_COUNT, encoding="utf-8"
        )
    except OSError:
        # never block startup over logging
        return None
    handler.setFormatter(formatter)
    setattr(handler, _MARK, True)
    root.addHandler(handler)
    return path
