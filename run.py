"""StockQuant 启动入口 — 仅做初始化 + 启动 Flask 服务"""
import os
import sys
import threading
import time
import webbrowser

import config as _cfg
from logger import get_logger

log = get_logger("stockquant")


def _wait_browse():
    """延迟自动打开浏览器, 等 Flask 就绪"""
    time.sleep(1.5)
    try:
        webbrowser.open(f"http://127.0.0.1:{_cfg.PORT}")
    except Exception:
        pass


def main():
    os.makedirs(_cfg.LOG_DIR, exist_ok=True)
    os.makedirs(_cfg.DATA_DIR, exist_ok=True)

    log.info("=" * 50)
    log.info("StockQuant v1.3.0 启动中...")
    log.info(f"  数据目录: {_cfg.DATA_DIR}")
    log.info(f"  日志目录: {_cfg.LOG_DIR}")
    log.info(f"  监听地址: http://{_cfg.HOST}:{_cfg.PORT}")
    log.info("=" * 50)

    from web_app import app

    if _cfg.OPEN_BROWSER:
        threading.Thread(target=_wait_browse, daemon=True).start()

    app.run(host=_cfg.HOST, port=_cfg.PORT, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
