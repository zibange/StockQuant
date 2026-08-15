# Last modified: 2026-08-12 21:30:00
"""集中配置模块 — 数据目录 / TQ 地址 / 服务端口 / 日志目录

所有硬编码路径与地址收敛于此, 支持环境变量覆盖, 便于迁移与打包。
环境变量清单:
  TDX_BASE_DIR     项目根目录 (默认: exe 所在目录 或 当前工作目录)
  TDX_DATA_DIR     数据目录   (默认: {BASE_DIR}/data)
  TDX_LOG_DIR      日志目录   (默认: {BASE_DIR}/logs)
  TDX_TQ_URL       通达信 TQ-Local JSON-RPC 地址 (默认: http://127.0.0.1:17709/)
  TDX_TQ_TIMEOUT   TQ 请求超时秒数 (默认 60)
  TDX_TQ_MAX_RETRY TQ 请求重试次数 (默认 2)
  TDX_INSTALL_DIR  通达信安装目录 (设置后跳过注册表定位)
  HOST / PORT      Flask 监听地址与端口 (默认 0.0.0.0:8765)
  TDX_OPEN_BROWSER 启动后是否自动打开浏览器 (默认 1, 设 0 关闭)
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def _default_base_dir() -> Path:
    """打包模式 (exe) 取 exe 所在目录, 源码模式取本文件所在目录 (不依赖 cwd)"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def _env_path(name: str, default: Path) -> Path:
    val = os.environ.get(name)
    return Path(val).expanduser() if val else default


# ---- 路径 ----
BASE_DIR = _default_base_dir()
DATA_DIR = _env_path("TDX_DATA_DIR", BASE_DIR / "data")
LOG_DIR = _env_path("TDX_LOG_DIR", BASE_DIR / "logs")

# ---- 服务 ----
HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8765"))
OPEN_BROWSER = os.environ.get("TDX_OPEN_BROWSER", "1") != "0"

# ---- 通达信 TQ-Local ----
TQ_URL = os.environ.get("TDX_TQ_URL", "http://127.0.0.1:17709/")
TQ_TIMEOUT = int(os.environ.get("TDX_TQ_TIMEOUT", "60"))
TQ_MAX_RETRY = int(os.environ.get("TDX_TQ_MAX_RETRY", "2"))

# ---- 通达信安装目录 (设置后跳过 winreg 注册表定位) ----
TDX_INSTALL_DIR = os.environ.get("TDX_INSTALL_DIR")
