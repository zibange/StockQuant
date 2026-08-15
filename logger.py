# Last modified: 2026-08-12 21:30:00
"""统一日志模块 — 按日期滚动，同时输出到控制台和 logs/ 目录"""
from __future__ import annotations

import logging
import sys
from logging.handlers import TimedRotatingFileHandler

from config import LOG_DIR

_LOG_DIR = LOG_DIR
_LOG_DIR.mkdir(parents=True, exist_ok=True)

_FMT_CONSOLE = "%(asctime)s [%(levelname)-7s] %(name)s  %(message)s"
_FMT_FILE    = "%(asctime)s [%(levelname)-7s] %(name)s %(filename)s:%(lineno)d  %(message)s"
_DATE_FMT    = "%Y-%m-%d %H:%M:%S"

_initialized = False


def _init_once():
    global _initialized
    if _initialized:
        return
    _initialized = True

    root = logging.getLogger("tdxlambda")
    root.setLevel(logging.DEBUG)
    root.propagate = False

    # 控制台（INFO 及以上）
    _ch = logging.StreamHandler(sys.stdout)
    _ch.setLevel(logging.INFO)
    _ch.setFormatter(logging.Formatter(_FMT_CONSOLE, _DATE_FMT))
    root.addHandler(_ch)

    # 文件（DEBUG 及以上，按天滚动，保留 30 天）
    _fh = TimedRotatingFileHandler(
        _LOG_DIR / "tdxlambda.log",
        when="midnight",
        interval=1,
        backupCount=30,
        encoding="utf-8",
    )
    _fh.setLevel(logging.DEBUG)
    _fh.setFormatter(logging.Formatter(_FMT_FILE, _DATE_FMT))
    _fh.suffix = "%Y-%m-%d.log"
    root.addHandler(_fh)

    # 屏蔽第三方库噪音
    for _n in ("werkzeug", "urllib3", "matplotlib"):
        logging.getLogger(_n).setLevel(logging.WARNING)


def get_logger(name: str | None = None) -> logging.Logger:
    _init_once()
    return logging.getLogger("tdxlambda" + (f".{name}" if name else ""))
