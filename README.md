# StockQuant — A股量化研究系统 · 全面文档

> **文档版本**：v6.0  **更新日期**：2026-08-15  **适用阶段**：Phase 6（打包交付 + 数据源兼容加固 + 性能优化），面向开源二次开发与部署运维

---

## 目录

- [1. 项目概述](#1-项目概述)
  - [1.1 是什么](#11-是什么)
  - [1.2 核心能力矩阵](#12-核心能力矩阵)
  - [1.3 代码规模](#13-代码规模)
  - [1.4 开发阶段](#14-开发阶段)
- [2. 目录结构](#2-目录结构)
- [3. 技术架构](#3-技术架构)
  - [3.1 全栈分层图](#31-全栈分层图)
  - [3.2 模块依赖关系](#32-模块依赖关系)
  - [3.3 技术选型与理由](#33-技术选型与理由)
- [4. 源码模块详解](#4-源码模块详解)
  - [4.1 config.py](#41-configpy--集中配置)
  - [4.2 logger.py](#42-loggerpy--统一日志)
  - [4.3 cache.py](#43-cachepy--统一缓存组件)
  - [4.4 run.py](#44-runpy--启动入口)
  - [4.5 tdx_tq_client.py](#45-tdx_tq_clientpy--通达信-http-客户端)
  - [4.6 stock_app.py](#46-stock_apppy--行情--交易)
  - [4.7 quote_service.py](#47-quote_servicepy--行情查询服务)
  - [4.8 kline_service.py](#48-kline_servicepy--k-线服务)
  - [4.9 fundamental_store.py](#49-fundamental_storepy--基本面存储层)
  - [4.10 fundamental_service.py](#410-fundamental_servicepy--基本面同步服务)
  - [4.11 stock_screener.py](#411-stock_screenerpy--选股引擎)
  - [4.12 predict_service.py](#412-predict_servicepy--预测服务)
  - [4.13 fundamental_fields.py](#413-fundamental_fieldspy--字段元数据)
  - [4.14 web_app.py](#414-web_apppy--flask-应用本体)
- [5. 数据模型](#5-数据模型)
  - [5.1 存储总览](#51-存储总览)
  - [5.2 market.duckdb + Parquet](#52-marketduckdb--parquet)
  - [5.3 fundamental.duckdb](#53-fundamentalduckdb)
  - [5.4 portfolio.duckdb](#54-portfolioduckdb)
- [6. API 参考](#6-api-参考)
  - [6.1 通用约定](#61-通用约定)
  - [6.2 行情接口](#62-行情接口)
  - [6.3 基本面接口](#63-基本面接口)
  - [6.4 自动同步接口](#64-自动同步接口phase-5-新增)
  - [6.5 选股器接口](#65-选股器接口)
  - [6.6 缓存管理接口](#66-缓存管理接口)
  - [6.7 交易接口](#67-交易接口)
  - [6.8 预测接口](#68-预测接口)
  - [6.9 认证与管理接口](#69-认证与管理接口)
- [7. 前端架构](#7-前端架构)
- [8. 典型流程](#8-典型流程)
- [9. 缓存体系](#9-缓存体系)
- [10. 基本面自动同步](#10-基本面自动同步)
- [11. 打包与部署](#11-打包与部署)
  - [11.1 打包模式决策](#111-打包模式决策)
  - [11.2 PyInstaller 打包](#112-pyinstaller-打包)
  - [11.3 程序图标](#113-程序图标)
  - [11.4 迁移到其他设备](#114-迁移到其他设备)
  - [11.5 运行验证](#115-运行验证)
- [12. 二次开发指南](#12-二次开发指南)
- [13. 故障排查](#13-故障排查)
- [附录 A：API 字段名速查](#附录-aapi-字段名速查)
- [附录 B：版本历史](#附录-b版本历史)
- [附录 C：Phase 5/6 变更清单](#附录-cphase-56-变更清单)

---

## 1. 项目概述

### 1.1 是什么

StockQuant 是面向 A 股个人投资者的**本地运行**量化研究与模拟交易系统。所有数据来自本机通达信客户端（17709 端口 HTTP JSON-RPC），用 DuckDB + Parquet 做本地数据仓，Flask 提供 REST API 和 Web 界面。

核心设计哲学：**零云依赖、零 Key、零外网行情**。通达信客户端（TdxW.exe）是唯一外部数据源。

从 Phase 1 到 Phase 6 的演进轨迹：

```
Phase 1 (08-10)    → 行情四图联动 + 模拟交易 + 因子预测 + 行业大盘
Phase 2 (08-11)    → 基本面处理（438 财务 + 46 GP + 筹码 + 十大股东）+ 前后端分离
Phase 3 (08-13)    → 选股引擎（StockScreener + KDJStrategy 策略模式）+ config/logger 基础设施
Phase 4 (08-14)    → 多级缓存（cache.py TTLCache + CacheBus）+ Service 分层 + 前端 sessionStorage 秒显 + 异常加固
Phase 5 (08-15)    → run.py 启动入口 + 基本面自动同步(auto_sync) + 管理后台增强 + 优雅退出 + 前端状态保留
Phase 6 (08-15)    → TQ 返回格式兼容加固（_unwrap / sub.empty / _extract_code_df / _normalize_codes）+ PyInstaller 打包交付 + 图标 + 性能验证
```

### 1.2 核心能力矩阵

| 能力域 | 子模块 | 数据源 | 存储 | 前端入口 |
|--------|--------|--------|------|----------|
| **行情研究** | K 线（日/周/月）、MA5/10/20/60、MACD、KDJ、四图联动、全屏视图 | TDX TQ-Local | Parquet + market.duckdb | `GET /` |
| **行业大盘** | 通达信行业/概念/地区板块聚合（industry/concept/regional） | sector_list + stock_list_in_sector | **内存缓存 60s（quote_service）** | 行业大盘 Tab |
| **模拟交易** | 注册/登录、自选、买入（佣金0.03%）、卖出（佣金+印花税0.1%）、持仓、流水 | TDX 实时快照 | portfolio.duckdb | 交易 Tab |
| **基本面 · 公司信息** | 股票基础 + 动态指标（PE/PB/股息率/总市值） | get_stock_info + get_more_info | fundamental.duckdb | 基本面 Tab → 公司信息 |
| **基本面 · 财务报表** | 438 财务指标宽表/长表双查，支持报告期筛选 | get_financial_data(table_list=...) | financial_facts（EAV） | 基本面 Tab → 财务数据 |
| **基本面 · 筹码/L2/GP** | 股东户数、筹码分布、L2 行情 | get_gpjy_value / formula_process / get_exday_data | gpjy_facts / chip_facts / l2_facts | 基本面 Tab → 交易指标/筹码/L2 |
| **基本面 · 十大股东** | 十大股东 + 十大流通股东 | download_file(down_type=1) JSON 解析 | shareholder_facts | 基本面 Tab → 股东明细 |
| **基本面 · 主营构成** | 主营构成明细（按产品/行业/地区）+ 概述 | download_file(down_type=5) JSON 解析 | mainbusi_facts + mainbusi_profile | 基本面 Tab → 主营构成 |
| **基本面 · 自动同步** | **★ Phase 5：7 种业务批量自动同步 + 启动执行 + SSE 进度** | TDX 全接口 | auto_sync.json 配置 | 自动同步配置面板 |
| **选股引擎** | 市值桶（C1~C6）→ 策略评估（KDJ 金叉/死叉）→ Top N | TDX 实时 + K 线 | **后端 30s TTL + 前端 sessionStorage** | `GET /screener`（**秒显**） |
| **预测** | 因子加权评分（PE/PB/Beta/股息率/换手率）+ 买卖建议 | TDX 行情 | 无持久化 | 预测 Tab |
| **系统管理** | 服务状态、TDX 测试、API 开关、库数据检查/删除、缓存管理、流向图 | 本地检测 | - | `GET /admin` |

### 1.3 代码规模

| 文件 | 体积 | 行数 | 职责 | Phase |
|------|------|------|------|-------|
| `web_app.py` | 99 KB | ~1976 | Flask 应用本体：~60 路由 + 缓存注册 + **自动同步调度 + 后台任务 SSE + 优雅退出** | 1-6 |
| `fundamental_service.py` | 60 KB | ~1237 | 基本面同步（股票/财务/GP/筹码/L2/股东/主营）+ 市值 | 2+5+6 |
| `stock_app.py` | 43 KB | ~881 | 行情 + 交易（KlineStore / PortfolioStore / StockApp）+ **通达信初始化** | 1+6 |
| `fundamental_store.py` | 39 KB | ~782 | DuckDB EAV 存储层 + 列名防御 + mainbusi_profile | 2+4+5 |
| `stock_screener.py` | 31 KB | ~661 | 选股引擎（策略模式）+ **市值桶 + 并发 K 线同步** | 3+6 |
| `fundamental_fields.py` | 23 KB | ~311 | 438 财务 + 46 GP 字段中文名 | 2 |
| `tdx_tq_client.py` | 21 KB | ~427 | 17709 端口 HTTP JSON-RPC 客户端 + **_unwrap / price_df 格式兼容** | 1+6 |
| `kline_service.py` | 13 KB | ~277 | K 线服务 + 120s TTL + 技术指标 + 估值评分 | 4 |
| `predict_service.py` | 8 KB | ~168 | 预测服务 + 因子评分 + 目标售价反推 | 4 |
| `quote_service.py` | 6 KB | ~131 | 板块行情 + 并行 + 60s TTL + **_normalize_codes** | 4+6 |
| `cache.py` | 4 KB | ~109 | TTLCache + CacheBus 统一缓存 | 4 |
| `config.py` | 2 KB | ~40 | 集中配置（路径/端口/TQ/环境变量覆盖） | 3 |
| `logger.py` | 2 KB | ~44 | 统一日志 | 3 |
| `run.py` | 1 KB | ~31 | **独立启动入口（初始化 + 自动开浏览器 + app.run）** | 5 |

**前端关键变更**：

| 文件 | 变更 |
|------|------|
| `fundamental.js` | **★ Phase 5：renderLong 报告期下拉状态保留（修复筛选后重置为"全部"）** |
| `admin.js` | ★ Phase 5：DuckDB 表卡片副标题文案校准 |
| `screener.js` | Phase 4：const→function 声明修复 TDZ；缓存优先渲染 |
| `screener.html` / `fundamental.html` | Phase 4：`?v=20260814a/b` 强制浏览器加载新脚本 |

### 1.4 开发阶段

| 阶段 | 内容 | 状态 |
|------|------|------|
| **Phase 1** | 行情四图 + 模拟交易 + 因子预测 + 行业大盘 + admin | ✅ |
| **Phase 2** | 基本面（438+46+筹码+股东）+ 前后端分离 | ✅ |
| **Phase 3** | 选股引擎 + config.py + logger.py | ✅ |
| **Phase 4** | cache.py + Service 分层 + 三级 TTL + sessionStorage 秒显 + except Exception 加固 + pivot_table 列名防御 | ✅ |
| **Phase 5** | run.py 启动入口 + 基本面自动同步(auto_sync.json) + 管理后台增强（库检查/安全清缓存/路由开关）+ 优雅退出 + 前端状态保留 | ✅ |
| **Phase 6** | **TQ 返回格式兼容加固（日线同步全量修复 + 速度 2 倍）+ PyInstaller onedir 打包 + 图标 + EXE 运行验证 + 交付清理** | ✅ |
| Phase 7 | 更多选股策略 + 回测框架 | 📋 |
| Phase 8 | AI 辅助点评 | 📋 |

---

## 2. 目录结构

```
tdx-lambda/
├── run.py                            ★ 启动入口（初始化 + 自动开浏览器 + app.run）
├── web_app.py                        Flask 本体（路由 + 缓存注册 + 自动同步调度 + 优雅退出）
├── auto_sync.json                    ★ 自动同步配置（业务开关 / 启动执行 / 上次结果，运行时生成）
├── cache.py                          统一缓存组件
├── quote_service.py                  行情查询 + 60s TTL + 并行
├── kline_service.py                  K 线 + 120s TTL + 指标 + 估值评分
├── predict_service.py                预测服务
├── stock_app.py                      行情 + 交易（KlineStore / PortfolioStore）+ 通达信初始化
├── tdx_tq_client.py                  17709 端口 HTTP JSON-RPC（唯一出口点）
├── config.py                         集中配置
├── logger.py                         统一日志
├── fundamental_store.py              EAV 存储层 + 列名防御 + mainbusi_profile
├── fundamental_service.py            基本面同步（含主营构成 + 市值）
├── stock_screener.py                 选股引擎（策略模式 + 市值桶 + 并发同步）
├── fundamental_fields.py             438+46 字段元数据
├── StockQuant.spec                   ★ PyInstaller 打包配置（onedir + 图标）
├── app.ico                           ★ 程序图标（由源图转换，多尺寸）
│
├── templates/
│   ├── index.html                    主页面
│   ├── fundamental.html              基本面页面（js ?v=20260814b）
│   ├── screener.html                 选股页面（js ?v=20260814a）
│   └── admin.html                    管理后台
│
├── static/
│   ├── js/
│   │   ├── app.js
│   │   ├── screener.js               function 声明 + 缓存优先
│   │   ├── fundamental.js            ★ 报告期下拉状态保留
│   │   └── admin.js
│   └── css/
│       ├── app.css / screener.css / fundamental.css / admin.css
│
├── data/                             ★ 运行时数据（源码模式在项目根；exe 模式在 exe 同级）
│   ├── market.duckdb                 K 线元数据（kline_meta + stock_info 市值）
│   ├── fundamental.duckdb            基本面（11 张表，~40 MB）
│   ├── portfolio.duckdb              模拟交易（users/positions/trades/watchlist）
│   ├── export/                       股东/筹码导出 CSV
│   └── kline/1d/*.parquet            日线缓存（~5500 只，zstd 压缩）
│
├── logs/                             ★ 运行日志（logger.py 按天滚动，保留 30 天）
├── build/StockQuant/                 打包中间产物
└── dist/StockQuant/                  打包产物（onedir，含 _internal 依赖，可整体迁移）
```

> 注：`batch_sync.py`（Phase 3 的批量同步脚本）已在 Phase 5 移除，其能力被 `_background_data_sync()`（web_app.py 内）+ 自动同步机制替代。

---

## 3. 技术架构

### 3.1 全栈分层图

```
┌─────────────────────────────────────────────────────────┐
│  浏览器端                                                │
│  sessionStorage（L1 秒显缓存）                             │
│  fetch API · ECharts · 原生 JS/CSS · Jinja2 模板           │
└────────────────────────┬────────────────────────────────┘
                         │ HTTP JSON
┌────────────────────────▼────────────────────────────────┐
│  Flask 服务层 · web_app.py                               │
│  ★ Phase 4: except Exception（零 HTML 500 泄漏）          │
│  ★ Phase 5: 自动同步调度 / 优雅退出 / 后台任务(SSE)        │
│  路由分组: 页面 /auth /kline /sector /screener             │
│          /cache /fundamental /auto-sync /portfolio       │
│          /predict /admin /sync /health                   │
└────────────────────────┬────────────────────────────────┘
                         │
┌────────────────────────▼────────────────────────────────┐
│  Service 层（Phase 4 从 stock_app.py 拆出）              │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐    │
│  │ QuoteService  │  │ KlineService  │  │PredictService │    │
│  │ sector()      │  │ get_kline()   │  │ predict()     │    │
│  │ ★ 60s TTL    │  │ ★ 120s TTL    │  │ 因子评分      │    │
│  │ ★ 16 workers │  │ MA/MACD/KDJ   │  │               │    │
│  └──────┬───────┘  └──────┬─────────┘  └──────┬───────┘    │
│         │                 │                    │          │
│  ┌──────▼─────────────────▼────────────────────▼────────┐ │
│  │  cache.py  TTLCache × 3  +  CacheBus（按 tag 失效）    │ │
│  │  quote.sector(60s) | kline(120s) | screener.*(30s)   │ │
│  └─────────────────────────────────────────────────────┘ │
│  ┌─────────────────────────────────────────────────────┐ │
│  │  _TaskManager（后台异步任务 + SSE 进度流）             │ │
│  │  ★ Phase 5: 自动同步 / 手动同步共用同一任务管道         │ │
│  └─────────────────────────────────────────────────────┘ │
└────────────────────────┬─────────────────────────────────┘
                         │
┌────────────────────────▼─────────────────────────────────┐
│  数据存储层 · DuckDB + Parquet                            │
│  market.duckdb / fundamental.duckdb / portfolio.duckdb   │
│  data/kline/1d/*.parquet                                  │
└────────────────────────┬─────────────────────────────────┘
                         │
┌────────────────────────▼─────────────────────────────────┐
│  通达信 TQ-Local  17709 端口 HTTP JSON-RPC               │
│  Windows + TdxW.exe（必须，唯一外部数据源）                │
└──────────────────────────────────────────────────────────┘
```

### 3.2 模块依赖关系

```
run.py                     ★ 启动入口
 └── import config.py, logger.py
     └── 延迟 import web_app.py（避免循环依赖）
         └── app.run(host, port)

web_app.py
 ├── import cache.py           ← 零依赖基础设施
 ├── import quote_service.py   ← Service 层
 ├── import kline_service.py
 ├── import predict_service.py
 ├── import stock_app.py       ← 通达信初始化 + 存储类
 ├── import fundamental_service.py
 ├── import fundamental_fields.py
 ├── import stock_screener.py
 ├── import config.py, logger.py
 └── import tdx_tq_client.py

Service 层（quote / kline / predict）
 ├── import cache.py           ← 统一缓存
 ├── import config.py
 ├── import logger.py
 └── 构造函数注入依赖（tq_client=None, kl_store=None）

fundamental_service.py
 ├── import tdx_tq_client.py
 ├── import fundamental_store.py
 └── import fundamental_fields.py

fundamental_store.py          ← 独立
 └── duckdb + pandas

tdx_tq_client.py              ← 独立
 └── urllib.request（标准库，无第三方 HTTP 依赖）

config.py / logger.py / cache.py — 零运行时依赖基础设施
```

**设计原则**：
1. `tdx_tq_client.py` — 通达信接口唯一出口点
2. `cache.py` — 所有内存缓存统一实现，禁止各模块私有缓存
3. Service 构造函数接受可选依赖（方便单元测试 Mock）
4. 基础设施模块零互相依赖
5. **★ Phase 5**：`run.py` 只做初始化 + 启动；所有异步任务统一走 `_TaskManager`（支持 SSE 进度）
6. **★ Phase 5 硬约束**：数据库写操作在 Flask 主线程持有连接内完成；后台线程只做 HTTP 拉取与结果回传，避免多线程写 DuckDB 锁竞争
7. **★ Phase 6**：TQLocalClient 所有数据接口统一 `_unwrap` 解包 + `price_df` 兼容三种格式；同步任务并发期间临时抑制 TQ 日志等级避免锁竞争

### 3.3 技术选型

| 技术 | 用途 | 理由 |
|------|------|------|
| Python 3.10+ | 后端 | 通达信 SDK 只有 Python 绑定 |
| Flask 3.1.x | Web 框架 | 轻量，threaded=True 并发 |
| DuckDB 1.5.5 | OLAP | 列式嵌入式，比 SQLite 快 10-100x |
| PyArrow 25.0 | Parquet | zstd 压缩，按需加载 |
| Pandas 2.3.3 | 指标计算 + EAV PIVOT | rolling 算 MA/MACD/KDJ |
| urllib.request | HTTP JSON-RPC | 通达信 17709 纯 HTTP POST（标准库零依赖） |
| ECharts 5.x | 图表 | echarts.connect() 多图联动 |
| threading.Lock | 缓存线程安全 | TTLCache 内部锁 |
| ThreadPoolExecutor | 并行快照 | quote 16 workers / mv 8 / kline 4 |
| sessionStorage | 前端秒显 | 跨页面共享 |
| threading.Thread | 后台任务 | 自动同步 / SSE 任务管道 |
| atexit | 优雅退出 | 统一 CHECKPOINT + close |

---

## 4. 源码模块详解

### 4.1 config.py — 集中配置

```python
BASE_DIR         → exe 所在目录 / config.py 所在目录    # 源码模式不依赖 cwd
DATA_DIR         → {BASE_DIR}/data                     # TDX_DATA_DIR 覆盖
LOG_DIR          → {BASE_DIR}/logs                     # TDX_LOG_DIR 覆盖
HOST             → "0.0.0.0"                            # HOST 覆盖
PORT             → 8765                                 # PORT 覆盖
OPEN_BROWSER     → True                                 # TDX_OPEN_BROWSER=0 关闭
TQ_URL           → "http://127.0.0.1:17709/"            # TDX_TQ_URL 覆盖
TQ_TIMEOUT       → 60s                                  # TDX_TQ_TIMEOUT 覆盖
TQ_MAX_RETRY     → 2                                    # TDX_TQ_MAX_RETRY 覆盖
TDX_INSTALL_DIR  → None（设置后跳过注册表定位）
```

### 4.2 logger.py — 统一日志

按天滚动（保留 30 天），控制台 INFO+ / 文件 DEBUG+ 双输出。日志名空间 `tdxlambda.*`，屏蔽 werkzeug/urllib3/matplotlib 噪音。

```python
from logger import get_logger
log = get_logger(__name__)
log.info("完成: %d 条", count)
log.error("TDX 连接失败: %s", err)
```

### 4.3 cache.py — 统一缓存组件

**替换所有模块私有缓存类**，TTLCache 内部 `threading.Lock` 保护，带命中统计与 tag 注册。

```python
class TTLCache(default_ttl=60, name=""):
    get(key)           # 命中返回值，未命中返回 None（自动计 hits/misses）
    set(key, val, ttl=None)
    invalidate(key=None)  # 单 key 或全清
    info()             # {name, ttl, tags, size, hits, misses, hit_rate_pct, next_expiry_sec}

class CacheBus:                          # 单例 cache_bus
    register(cache, tags=[...])          # 给 cache 实例打标签
    invalidate_tags(*tags)               # 按标签批量清空，返回清理数量
    all_tags()

def all_cache_info() -> list[dict]       # 所有 TTLCache 实例快照（/api/cache/debug 用）
```

**已注册的缓存实例**：

| 缓存实例 | 所在模块 | TTL | Tag |
|----------|----------|-----|-----|
| `_sector_cache` | quote_service | 60s | `quote.sector` |
| `_cache` | kline_service | 120s | `kline` |
| `_route_cache` | web_app | 30s | `screener.status`, `screener.buckets`, `cache.freshness` |

### 4.4 run.py — 启动入口

独立启动脚本，统一初始化 + 启动：

```python
def main():
    os.makedirs(LOG_DIR, exist_ok=True)       # 日志目录
    os.makedirs(DATA_DIR, exist_ok=True)      # 数据目录
    log.info("StockQuant v1.3.0 启动中...")
    from web_app import app                   # 延迟导入，避免循环依赖
    if _cfg.OPEN_BROWSER:                     # 1.5s 后自动打开浏览器
        threading.Thread(target=_wait_browse, daemon=True).start()
    app.run(host=_cfg.HOST, port=_cfg.PORT, debug=False, use_reloader=False)
```

**与 `python web_app.py` 的差异**：

| 入口 | 适用 | 特点 |
|------|------|------|
| `python run.py` | 源码模式推荐 | 轻量：只初始化目录 + 启动；不触发自动同步调度 |
| `python web_app.py` | exe 打包 / 需要自动同步 | 含 `_fix_cwd_for_exe()` 切目录 + **auto_sync on_startup 调度** + 优雅退出注册 |

> 注：`on_startup` 自动同步只在 `web_app.py` 的 `if __name__ == "__main__"` 分支触发；若用 `run.py` 启动并需要自动同步，请在管理面板手动触发或改用 web_app.py 启动。

### 4.5 tdx_tq_client.py — 通达信 HTTP 客户端

通达信 17709 端口 HTTP JSON-RPC 封装，**唯一出口点**。

**★ Phase 6 格式兼容核心**：

```python
@staticmethod
def _unwrap(result):
    """解包底层返回: {ErrorId, Value} -> Value; 扁平结构去掉 ErrorId"""
    if isinstance(result, dict):
        if "Value" in result:
            return result["Value"]
        result = {k: v for k, v in result.items() if k != "ErrorId"}
    return result

def price_df(self, raw, field, column_names=None):
    """从 get_market_data 返回中抽取某字段 → DataFrame
    自动兼容三种输入格式:
      1) 已 unwrap (推荐): raw = {code: {Date:[], Open:[], Close:[], ...}}
      2) 未 unwrap:       raw = {Value: {code: {...}}, ErrorId: 0}
      3) 原生 tqcenter.tq 扁平格式: raw = {Open: DataFrame, Close: DataFrame}
    """
```

关键方法：

- `get_market_data(stock_list, period, count)` → K 线 `{code: {field: [...]}}`（**已 _unwrap**）
- `get_market_snapshot(stock_code)` → 实时行情快照（最新价/涨跌/五档）
- `get_stock_info(stock_code)` → 股票基础信息 dict
- `get_more_info(stock_code)` → 动态指标（PE/PB/流通市值）
- `get_match_stkinfo(key_word)` → 名称/代码模糊匹配
- `get_financial_data(tables, date, stock_code)` → 财务数据（table_list 透传，ProDataPaged 自动翻页）
- `get_gpjy_value(...)` → 股东交易指标
- `get_sector_list()` / `get_stock_list_in_sector(sector)` → 板块
- `download_file(stock_code, down_time, down_type)` → 落盘股东(1)/主营构成(5)文件
- `formula_process_*` → 筹码/技术指标公式计算

### 4.6 stock_app.py — 行情 + 交易 + 通达信初始化

**`_init_tdx()`（★ 打包关键）**：定位通达信 `PYPlugins/user` 并返回 tq 模块：

```python
def _init_tdx():
    """优先环境变量 TDX_INSTALL_DIR（跨平台/免注册表），否则按注册表卸载键扫描"""
    if TDX_INSTALL_DIR:
        sys.path.insert(0, os.path.join(TDX_INSTALL_DIR, "PYPlugins", "user"))
        from tqcenter import tq
        tq.initialize(__file__)
        return tq
    keys = [  # 依次尝试的注册表卸载键
        r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\通达信金融终端64",
        r"...\通达信专业版",
        r"...\通达信金融终端(量化模拟)",
        r"...\通达信iTendx研究终端",
        r"...\通达信金融终端(测试)",
    ]
    # 匹配则 sys.path.insert + from tqcenter import tq
```

3 个核心类：

| 类 | 职责 | 存储 |
|----|------|------|
| `KlineStore` | K 线 Parquet 读写（原子写 tmp+os.replace）+ market.duckdb（kline_meta + stock_info） | `data/market.duckdb` + `data/kline/1d/*.parquet` |
| `PortfolioStore` | 用户认证（salt+hash）、持仓、资金、流水、自选 | `data/portfolio.duckdb` |
| `StockApp` | 聚合门面：resolve / kline_chart / indicators / do_buy / do_sell / predict | - |

**KlineStore 关键实现**：
- `upsert`：按 date 去重新数据覆盖旧数据；**tmp 文件 + os.replace 原子写**；`row_group_size=5000` 分片（便于只读首尾行组）；检测复权口径混合并告警
- `upsert_many`：不同 code 写不同文件天然无冲突可并行，kline_meta 用 DataFrame 批量 INSERT（**避免单行事务 616x 差距**）

### 4.7 quote_service.py — 行情查询服务

```python
class QuoteService:
    __init__(tq_client=None)
    sector(sector_type="industry", period="1d")
        # 60s TTL + ThreadPoolExecutor(16) 并行快照
        # 支持 industry / concept / regional 三类板块
    quote(code)     # 单票实时行情（无缓存）
```

**★ Phase 6 修复 `_normalize_codes`**：TQLocalClient 返回 `[{Code, Name}]` dict 列表，原生 SDK 返回 str 列表，统一归一化：

```python
@staticmethod
def _normalize_codes(all_sectors):
    """兼容 dict 列表与 str 列表两种返回格式"""
    out = []
    for item in all_sectors:
        if isinstance(item, dict):
            code = item.get("Code") or item.get("code") or item.get("SectorCode")
            if code: out.append(code)
        elif isinstance(item, str):
            out.append(item)
    return out
```

板块代码过滤规则：industry=`881/882`，concept=`8805~8809`，regional=`8802`。

### 4.8 kline_service.py — K 线服务

```python
class KlineService:
    __init__(kl_store=None, tq_client=None)
    get_kline(code, period="1d", n=180, dividend="front",
              force=False, start=None, end=None)
    → {code, period, date[], open[], ..., ma5[], ..., dif/dea/macd, k/d/j,
       snap(实时快照), more(估值评分), latest, available_range}
```

- **120s TTL**：整体结果缓存（key=`code|period|n|dividend|start|end`），命中 ~10ms
- `_compute_indicators`：MA5/10/20/60 + MACD(12,26,9) + KDJ(9,3,3)
- `_fetch_from_tq`：**★ Phase 6 用 `sub.empty` 判断字段缺失**（替换 `if f in raw`，兼容 _unwrap 后格式），失败时本地兜底
- 估值评分系统：PE/PB/Beta/股息率/换手率五因子，每因子带 label/color/tip

### 4.9 fundamental_store.py — 基本面存储层

EAV 长表设计，**新增字段无需 ALTER TABLE**。**Phase 6 全量列表见 §5.3**（11 张表）。

关键防御（Phase 4）：`get_financial_wide` 校验 pivot_table 所需列名，缺失返回空表而非抛异常。

### 4.10 fundamental_service.py — 基本面同步服务

拉取 → 解析 → DuckDB upsert。同步方法矩阵：

| 方法 | 业务 | 默认参数 | 说明 |
|------|------|----------|------|
| `sync_stock_basic(codes, force)` | 基础信息 | - | 名称/行业/股本 |
| `sync_financial(codes, report_type)` | 专业财务 | report_time | 438 字段 |
| `sync_gpjy(codes, start, end)` | 交易专业数据 | - | 股东增减持 |
| `sync_chip(codes, days)` | 筹码指标 | days=250 | MCST/CYS/ASR/SCR/CYC |
| `sync_l2(codes, count)` | L2 扩展 | count=60 | 日线增量 |
| `sync_shareholder(codes, years)` | 十大股东 | years=5 | down_type=1 |
| `sync_mainbusi(codes, years)` | 主营构成 | years=3 | down_type=5 |
| `sync_mv(codes, force)` | 流通市值 | 7 天新鲜度跳过 | 选股分桶数据源 |

**★ Phase 6 sync_mv 性能设计**：
- 名称批量 upsert：DuckDB DataFrame 注册临时表 + INSERT..SELECT ON CONFLICT（**替代 executemany 单行事务 616x 慢**）
- 批量 skip 查询：一次 SQL 查 7 天内的 `stock_more`（替代 5553 次单票 SELECT）
- 并发抓取 8 线程 + BATCH_WRITE=200 批量写

### 4.11 stock_screener.py — 选股引擎

**市值分桶（6 档）**：

| 桶 | 名称 | 市值范围(亿) | 默认 |
|----|------|------------|------|
| C1 | 微盘股 | 0 ~ 20 | 排除 |
| C2 | 小盘股 | 20 ~ 50 | ✅ |
| C3 | 中盘股 | 50 ~ 100 | ✅ |
| C4 | 中大盘股 | 100 ~ 200 | ✅ |
| C5 | 大盘股 | 200 ~ 1000 | ✅ |
| C6 | 超大盘蓝筹 | 1000+ | ✅ |

**策略模式**：`BaseStrategy` → `KDJStrategy`（J>K 且 J>D 且 K>D 且 J 连续向上 N 天，按 J/J_slope/量比排序）。`list_strategies()` 枚举注册表。

**★ Phase 6 `_extract_code_df`**：兼容 unwrap 与未 unwrap 两种 TQ 返回格式：

```python
@staticmethod
def _extract_code_df(raw, code):
    """raw 兼容两种格式:
      1) 已 unwrap: {code: {"Open":[...], "Close":[...], "Date":[...], ...}}
      2) 未 unwrap: {"Value": {code: {...}}, "ErrorId": 0}
    """
    if "Value" in raw and isinstance(raw.get("Value"), dict):
        value = raw["Value"]
    else:
        value = raw
    code_data = value.get(code)
    ...
```

**日线同步 `sync_kline`（★ Phase 6 全量验证通过）**：
- **Phase 1 预筛**：`has_data` + 行数(≥count×0.8) + 通达信探测日期新鲜度，只抓需要补的批次（`_probe_tdx_latest_date`）
- **Phase 2 并发抓取**：KLINE_THREADS=4，worker 只做 HTTP；`upsert_many(workers=8)` 攒 BATCH_WRITE=300 批量并行写
- 并发期间 TQ 日志临时降为 WARNING（文件 handler 锁竞争）
- 实测：全量 ~5543 只 saved=5543, errors=10, **93~104s**（对比修复前 205.9s，约 2 倍提升）

**新鲜度检查 `check_cache_freshness`**（A+B 双校验）：kline_meta 众数日期 + 通达信日线探测，gap≤0=fresh / =1=stale / >1=stale。

### 4.12 predict_service.py — 预测服务

- `predict(code)`：MA5/MA20 + MACD 评分（±2/±2/±1）→ 偏多/偏空/中性 + 最近 8 次金叉死叉
- `predict_sell(user_id, code, target_r, add_qty, ...)`：目标收益率反推售价
  - 模式1（不加仓）：`B = A × (1+C)`
  - 模式2（加仓）：`B₂ = (N₁·A + N₂·B₁) × (1+C) / (N₁+N₂)`
  - 无 DB 持仓时支持手动输入 `n1`/`a` 虚拟预测

### 4.13 fundamental_fields.py — 字段元数据

FN_NAME 438 项（FN1 基本每股收益 …）+ GP_NAME 46 项（GP01 股东户数 …）。EAV 模式天然兼容新增字段。

### 4.14 web_app.py — Flask 应用本体

**职责**：HTTP 路由 + 参数解析 + 认证守卫 + 页面渲染 + 启动守护 + **缓存注册 + 自动同步调度 + 后台任务 + 优雅退出**。**不写业务逻辑**。

**Phase 5 新增机制**：

1. **自动同步配置管理**：`_load_auto_sync_config` / `_save_auto_sync_config`（临时文件 + os.replace 原子替换，损坏回退默认）
2. **后台任务管理器 `_TaskManager`**：`submit()` 提交（daemon 线程 + 队列），SSE 进度流 `stream(tid)`（30s 心跳 + keep-alive），10 分钟自动清理
3. **优雅退出**：atexit → 三个 DuckDB 连接依次 `CHECKPOINT + close` + TQ close
4. **安全缓存清理**：`clear-all-cache` 用进程内已有连接 DELETE + CHECKPOINT（详见 §13）
5. **`_fix_cwd_for_exe`（★ Phase 6 打包关键）**：frozen 模式 cwd 可能是 `C:\Windows\System32`，强制 `os.chdir(exe 所在目录)`

---

## 5. 数据模型

### 5.1 存储总览

| 存储 | 引擎 | 位置 | 用途 | 写入频率 |
|------|------|------|------|----------|
| K 线 | Parquet + DuckDB | data/kline/1d/*.parquet | 日线 OHLCV + 指标 | 每日收盘后增量 |
| 行情元索引 | DuckDB | data/market.duckdb | kline_meta + 股票列表（市值） | 同 K 线 |
| 基本面 | DuckDB | data/fundamental.duckdb | 11 张表（~40 MB） | 同步时批量 upsert |
| 模拟交易 | DuckDB | data/portfolio.duckdb | users/positions/trades/watchlist | 实时 |

**DuckDB 模式**：duckdb.connect(path) — 文件锁独占，多线程 OK（内部串行），**多进程会冲突**。★ 硬约束：所有连接主线程单例持有，写操作统一 CHECKPOINT。

### 5.2 market.duckdb + Parquet

- **kline_meta**：`code (PK), period, last_date, row_count, updated_at`
- **Parquet schema**：`date, code, period, dividend_type, Open, High, Low, Close, Volume, Amount`（zstd，row_group_size=5000）
- 复权口径统一 `front`（前复权）

### 5.3 fundamental.duckdb

**EAV 长表**：`(code, report_date/trade_date, field_code, value)` 四元组。

| 表名 | 主键 | 类型 |
|------|------|------|
| stock_info | code | 宽表 |
| stock_more | code | 宽表（市值/估值/资金流） |
| financial_facts | (code, report_date, field_code) | EAV 长表 |
| gpjy_facts | (code, trade_date, field_code) | EAV 长表 |
| chip_facts | (code, trade_date, field_code) | EAV 长表 |
| l2_facts | (code, trade_date, field_code) | EAV 长表 |
| shareholder_facts | (code, report_date, holder_type, rank) | 宽表 |
| mainbusi_facts | (code, report_date, field_code) | EAV 长表 |
| mainbusi_profile | code | 宽表（主营概述：产品名/业务描述） |
| field_meta | field_code | 字典表 |
| update_log | id | 日志表 |

### 5.4 portfolio.duckdb

users / position / trade_log / watchlist。费率：买入 fee = max(5, amount × 0.0003)；卖出同 + stamp_tax = amount × 0.001。密码 salted hash 存储。

---

## 6. API 参考

### 6.1 通用约定

基础地址：http://127.0.0.1:8765/

成功：`{"ok": true, "data": ...}`  /  错误：`{"error": "<描述>"}`

**异常策略**：所有异常（RuntimeError / ValueError / KeyError / DuckDB IOException）统一返回 JSON + HTTP 500。数据安全：`_rows_json` / `_clean_nan` 将 NaN 递归转 null。

### 6.2 行情接口

| 接口 | 参数 | 缓存 |
|------|------|------|
| GET /api/version | - | - |
| GET /health | - | - |
| GET /api/search | q, limit | - |
| GET /api/kline | code, period=1d, n=180, div=front, refresh, start, end | **120s TTL** |
| GET /api/quote | code | - |
| GET /api/sector | type=industry\|concept\|regional, period=1d | **60s TTL + 16 workers 并行** |

### 6.3 基本面接口

| 接口 | 参数 |
|------|------|
| GET /api/fundamental/profile | code |
| GET /api/fundamental/financial | code, format=wide\|long, report_date, fields, page, page_size |
| POST /api/fundamental/sync | JSON: {codes, biz, async, ...} |
| GET /api/fundamental/summary | -（各表记录数 + 最近 10 条日志） |
| GET /api/fundamental/fields | all=1 |
| GET /api/fundamental/gpjy \| chip \| l2 \| trace \| synced | code |
| GET /api/fundamental/shareholder \| mainbusi | code, report_date, holder_type |

**`POST /api/fundamental/sync` 请求体**：

```json
{
  "codes": ["600519.SH", "000002.SZ"],
  "biz": "financial",             // basic | financial | gpjy | chip | l2 | shareholder | mainbusi
  "async": true,                  // true → 返回 task_id + SSE 进度
  "days": 250,                    // chip 回看天数
  "count": 60,                    // l2 拉取条数
  "years": 5,                     // shareholder/mainbusi 回看年数
  "force": false,
  "start_time": null, "end_time": null
}
```

### 6.4 自动同步接口

| 接口 | 方法 | 参数 | 说明 |
|------|------|------|------|
| GET /api/fundamental/auto-sync/config | - | 无 | 读取配置（含 last_run / last_result） |
| POST /api/fundamental/auto-sync/config | - | JSON 部分字段 | 保存 enabled / on_startup / delay_seconds / biz 各开关 |
| POST /api/fundamental/auto-sync/run | - | 无 | 手动执行 → `{task_id, stream_url}`（需 enabled 且至少一个 biz） |

### 6.5 选股器接口

| 接口 | 参数 | 缓存 |
|------|------|------|
| GET /api/screener/status | - | **30s TTL** |
| GET /api/screener/buckets | - | **30s TTL** |
| GET /api/screener/strategies | - | - |
| POST /api/screener/sync | JSON: {biz: all\|mv\|kline, force, kline_count} | - |
| GET /api/screener/task/\<tid\> | - | - |
| GET /api/screener/task/\<tid\>/stream | - | SSE 流式 |
| GET /api/screener/pick | top_n, buckets, strategy, strategy_cfg | - |

### 6.6 缓存管理接口

| 接口 | 参数 | 返回 |
|------|------|------|
| GET /api/cache/debug | - | `{ok, caches: [{name, hits, misses, hit_rate_pct, size, tags}]}` |
| GET /api/cache/freshness | - | Parquet 最新时间 + DuckDB 最新行（A+B 双校验，30s TTL） |
| POST /api/cache/invalidate | JSON: `{"tags": ["kline", "quote.sector"]}` | `{ok, cleared: N}` |

**CacheBus 预置 tag**：`quote.sector` / `kline` / `screener.status` / `screener.buckets` / `cache.freshness`。

### 6.7 交易接口（需 session）

GET/POST /api/portfolio | /api/portfolio/buy | /api/portfolio/sell | /api/portfolio/cash | /api/portfolio/trades | /api/watchlist

费率：买入 fee = max(5, amount × 0.0003)；卖出 fee + stamp_tax = amount × 0.001。

### 6.8 预测接口

| 接口 | 说明 |
|------|------|
| GET /api/predict | code → 买入评分（score, direction, factors, reason） |
| GET /api/predict-sell | code + target_r + add_qty + add_price + n1/a → 卖出建议（两种模式） |

### 6.9 认证与管理接口

| 接口 | 说明 |
|------|------|
| POST /api/auth/register / login / logout | 注册 / 登录 / 登出 |
| GET /api/auth/me / users | 当前用户 / 用户列表 |
| GET /api/admin/status | 服务状态（TDX 连通、数据量、缓存 hit_rate）15s 缓存 |
| GET /api/admin/flow | 数据流拓扑图（nodes/edges） |
| GET /api/admin/db-detail | K 线库详情（单票范围 / 全量 meta） |
| POST /api/admin/db-delete | 删除单票或全量 K 线 |
| POST /api/admin/clear-memory | DuckDB flush + CHECKPOINT |
| POST /api/admin/clear-all-cache | 清空 Parquet + 业务表（安全策略见 §13.3） |
| POST /api/admin/toggle | 启用/禁用某 Endpoint（路由开关） |
| POST /api/admin/tq-test | 后台测试任意 TQ 接口 |
| POST /api/admin/shutdown / restart | 远程关停 / 重启提示 |
| GET /api/sync/status | 同步引擎状态 |

---

## 7. 前端架构

### 7.1 整体结构

```
templates/
  index.html        ← 主页面
  fundamental.html  ← 基本面页面
  screener.html     ← 选股页面
  admin.html        ← 管理后台

static/js/
  app.js            ← K 线 + 行业大盘 + 交易
  screener.js       ← function 声明 + 缓存优先
  fundamental.js    ← ★ 报告期下拉状态保留
  admin.js          ← 管理后台
```

### 7.2 选股页秒显机制（L1 + L2 双缓存）

```
用户进入 /screener
    │
    ├── sessionStorage.getItem("screener.buckets")   ← L1
    │     命中 → 秒显（0ms）
    └── 后台 fetch("/api/screener/buckets")          ← L2
          ├── _route_cache 30s TTL 命中 → ~10ms
          ├── 未命中 → 计算 → set + 写 sessionStorage
          └── 更新视图（覆盖缓存数据）
```

sessionStorage Key：`screener.buckets` / `screener.status` / `screener.freshness`。

### 7.3 基本面报告期筛选状态保留（Phase 5 修复）

**问题**：长表筛选报告期后，下拉异步加载完成时重置为"全部"，表格数据却按旧报告期展示。

**修复**（fundamental.js `renderLong`）：

```javascript
// 1. 渲染开始时保存当前选中值
const cur = document.getElementById("date-select-long").value;
// 2. option 模板输出 selected
`<option value="${d}" ${d === cur ? "selected" : ""}>${d}</option>`
// 3. all_dates 异步回调中恢复选中值
sel.value = cur;
```

---

## 8. 典型流程

### 8.1 启动流程

```
run.py / web_app.py __main__
    ├── _fix_cwd_for_exe()                        # 仅 exe 模式（★ Phase 6）
    ├── 通达信初始化（_init_tdx → 注册表/TDX_INSTALL_DIR 定位）→ 行情连通性自检
    ├── 存储层初始化（KlineStore / PortfolioStore / FundamentalService）
    ├── Service 层初始化 + 缓存注册
    │   ├── QuoteService(tq_client)  + _sector_cache(60s) + register("quote.sector")
    │   ├── KlineService(kl_store, tq_client) + _cache(120s) + register("kline")
    │   ├── PredictService(tq_client)
    │   └── _route_cache(30s) + register("screener.status", "screener.buckets", "cache.freshness")
    ├── atexit 注册 _graceful_close()
    ├── _auto_open_browser()                      # OPEN_BROWSER
    ├── auto_sync on_startup → _auto_sync_run_async()   # 仅 web_app.py 入口
    └── app.run(threaded=True)
```

### 8.2 K 线数据获取

```
GET /api/kline?code=600519
    │
    ▼
api_kline() — web_app.py（except Exception → JSON 500）
    ▼
KlineService.get_kline()
    ├── _cache.get(key)       # 120s TTL，命中 ~10ms
    └── 未命中:
        ├── kl_store.load(code) → Parquet
        ├── 补最新 N 条: tq_client.get_market_data()（★ sub.empty 防御）
        ├── Pandas rolling 计算 MA/MACD/KDJ
        ├── 实时快照 + 估值评分（并行，失败降级空 dict）
        └── _cache.set(key, result)
```

### 8.3 行业行情快照

```
GET /api/sector?type=industry
    │
    ▼
QuoteService.sector()
    ├── _sector_cache.get(key) → 命中（60s TTL, ~3ms）
    └── 未命中:
        ├── get_sector_list() → 板块列表（★ _normalize_codes 归一化）
        ├── _filter_sector_codes() → industry(881/882) / concept(8805-9) / regional(8802)
        ├── ThreadPoolExecutor(16) 并行逐板块快照
        ├── 排序（周期收益或当日涨幅）
        └── _sector_cache.set(key, result)
```

### 8.4 选股完整链路

```
POST /api/screener/sync {biz:"all"} → _TaskManager 异步（SSE 进度）
    ├── sync_mv：名称批量 upsert + 7 天新鲜度预筛 + 8 线程并发 get_more_info + BATCH_WRITE=200
    └── sync_kline：探针新鲜度 → 预筛批次 → 4 线程抓取 → upsert_many 批量写

GET /api/screener/pick?top_n=10&strategy=kdj
    ├── query_mv_candidates(allowed_buckets, exclude_st)
    ├── 逐票：load_tail → 过滤（上市天数/成交额/停牌）→ KDJStrategy.evaluate
    ├── sort_key 排序 → Top N
    └── 返回 + 前端 sessionStorage 秒显
```

### 8.5 基本面同步 → 缓存失效

```
POST /api/fundamental/sync → sync_financial(codes)
    ├── 拉取 438 财务指标
    ├── fundamental_store.upsert_financial(code, df)
    ├── store.log_update(...)
    └── cache_bus.invalidate_tags("kline", "quote.sector", "screener.*")
```

### 8.6 自动同步执行链路

```
POST /api/fundamental/auto-sync/run（或启动 on_startup）
    │
    ├── 校验 enabled 且至少一个 biz 开启
    ▼
_background_data_sync(codes=None, biz_list)
    ├── codes = stock_info 表 DISTINCT code（库空则跳过）
    ├── 按 biz_list 顺序逐业务同步（每步 progress 回调推 SSE）
    │     stock_basic → sync_stock_basic
    │     financial  → sync_financial
    │     gpjy       → sync_gpjy
    │     chip       → sync_chip(days=250)
    │     l2         → sync_l2(count=60)
    │     shareholder→ sync_shareholder(years=5)
    │     mainbusi   → sync_mainbusi(years=3)
    ▼
_on_done（仅手动路径）/ 线程尾部
    ├── last_run = 当前时间
    ├── last_result = 各业务摘要（剔除 per_code 明细）
    └── _save_auto_sync_config() → 原子写回 auto_sync.json
```

### 8.7 交易流程

```
POST /api/portfolio/buy {code, price, qty}
    ├── _require_user() → user_id
    ├── PortfolioStore.get_user → cash
    ├── TDX get_market_snapshot(code) → current_price
    ├── fee = max(5, amount * 0.0003)   # 买入佣金
    ├── 校验 cash >= amount + fee
    ├── PortfolioStore.buy(...)
    └── 返回 {ok, cost, fee, cash_remaining}
```

### 8.8 优雅退出

```
Ctrl+C / 关闭进程
    │
    └── atexit → _graceful_close()
          ├── kline_store  : CHECKPOINT + close（market.duckdb）
          ├── portfolio    : CHECKPOINT + close（portfolio.duckdb）
          ├── fundamental  : CHECKPOINT + close（fundamental.duckdb）
          └── tq_client    : close()
    → 避免 Windows 残留 .wal 文件导致下次启动写事务"拒绝访问"
```

---

## 9. 缓存体系

### 9.1 三级缓存架构

```
┌─────────────────────────────────────────────────┐
│ L1 浏览器 sessionStorage                         │
│ Key: screener.buckets / status / freshness      │
│ 作用: 跨页面秒显                                 │
└──────────────────────────┬──────────────────────┘
                           │ 首次 fetch / 后台刷新
┌──────────────────────────▼──────────────────────┐
│ L2 后端 TTLCache（进程内内存）                    │
│ quote.sector  → 60s  (板块行情快照)               │
│ kline         → 120s (K线指标计算结果)            │
│ screener.*    → 30s  (选股状态/分桶/新鲜度)       │
└──────────────────────────┬──────────────────────┘
                           │ cache miss
┌──────────────────────────▼──────────────────────┐
│ L3 数据源                                        │
│ TDX TQ-Local :17709 (实时行情)                   │
│ DuckDB               (基本面长表)                 │
│ Parquet files        (历史K线)                   │
└─────────────────────────────────────────────────┘
```

### 9.2 性能基准

| 接口 | 优化前 | 优化后（冷） | 缓存命中 | 提升 |
|------|--------|------------|---------|------|
| `/api/sector` | 2581 ms | ~1895 ms（并行化） | **3 ms** | 860× |
| `/api/kline` | 206 ms | 206 ms | **10 ms** | 20× |
| `/api/screener/buckets` | 300+ ms | - | **10 ms** | 30× |
| `/api/screener/status` | 21,870 ms | - | **10 ms** | 2187× |
| 选股页切换白屏 | 4s+ | - | **746ms 秒显** | 5× |
| 日线全量同步 | 205.9s（8-13） | **93~104s**（★ Phase 6） | - | **~2×** |

### 9.3 CacheBus 使用模式

```python
from cache import TTLCache, cache_bus

_my_cache = TTLCache(default_ttl=60, name="my_feature")
cache_bus.register(_my_cache, tags=["my_feature"])

_my_cache.get("key")
_my_cache.set("key", value)
cache_bus.invalidate_tags("my_feature")
```

---

## 10. 基本面自动同步

### 10.1 配置模型（auto_sync.json）

```json
{
  "enabled": false,            // 总开关
  "biz": {                     // 7 种业务开关
    "stock_basic": false,
    "financial": false,
    "gpjy": false,
    "chip": false,
    "l2": false,
    "shareholder": false,
    "mainbusi": false
  },
  "on_startup": false,         // 启动时自动执行
  "delay_seconds": 3,          // 启动后延迟秒数
  "last_run": null,            // 上次执行时间（自动回写）
  "last_result": null          // 上次各业务同步结果（自动回写）
}
```

写入方式：临时文件 + `os.replace` 原子替换；读取失败回退默认配置。

### 10.2 触发方式对比

| 触发 | 路径 | 特点 |
|------|------|------|
| 启动时 | `web_app.py __main__` → on_startup → `_auto_sync_run_async()` | 延迟 delay_seconds 秒，后台线程 |
| 手动 | `POST /api/fundamental/auto-sync/run` | 走 `_TaskManager`，SSE 进度，`_on_done` 回写结果 |
| 手动（单业务） | `POST /api/fundamental/sync` `{codes, biz, async}` | 按 codes 拉取，支持 async 模式 |

### 10.3 约定与注意

- `codes=None` 时从 `stock_info` 表取全库股票；库空则跳过
- 每次同步 `last_result` 保存摘要（排除 per_code 明细，控制 JSON 体积）
- 数据库写操作全部在主线程持有的连接中完成，后台线程只做 HTTP 拉取 + 进度回调（**硬约束**）

---

## 11. 打包与部署

### 11.1 打包模式决策

| 模式 | 说明 | 优点 | 缺点 |
|------|------|------|------|
| **onedir（当前）** | exe + `_internal\` 依赖目录 | 启动快（冷启动 ~20s），杀软误报低，调试方便 | 目录多，拷贝需压缩 |
| onefile | 单文件，运行时自解压 `%TEMP%\_MEIxxxx` | 单文件分发 | **启动多 10-30s 解压 266MB**、杀软误报高、临时目录双份占用 |

> **结论**：本项目保持 onedir。运行依赖约 266MB（pyarrow 80MB 大头），无法合并的原因是通达信外部进程（17709 数据源）与运行时数据目录物理上不可能打进 exe。迁移时压缩包拷贝即可，收益等同单文件。

### 11.2 PyInstaller 打包

```powershell
# 依赖
pip install pyinstaller
# 打包（读取 StockQuant.spec）
pyinstaller StockQuant.spec
# 产物: dist/StockQuant/StockQuant.exe（9.6MB）+ _internal\（依赖）
```

**StockQuant.spec 要点**：

```python
a = Analysis(['run.py'],              # 入口（比 web_app.py 更轻量）
    datas=[('templates', 'templates'), ('static', 'static')],
    hiddenimports=['duckdb._sqltypes'],   # duckdb 运行时动态导入
    excludes=['matplotlib', 'scipy', 'IPython', 'jupyter', ...],  # 砍体积
)
exe = EXE(pyz, a.scripts, [],
    name='StockQuant', console=False,   # 无黑色控制台窗口
    icon='app.ico')                     # 程序图标
coll = COLLECT(exe, a.binaries, a.zipfiles, a.datas, name='StockQuant')
```

### 11.3 程序图标

`app.ico` 由源图（JPG 2048×2048，`StockQuant 程序图标（无水印）.jpg`）经 Pillow 转换生成，包含 256/128/64/48/32/24/16 七种尺寸。已用 pefile 验证 RT_ICON 资源嵌入正常。

### 11.4 迁移到其他设备

**前提**：新设备必须安装通达信客户端（唯一数据源），并登录。

| 迁移内容 | 操作 |
|----------|------|
| 程序本体 | 拷贝 `dist\StockQuant\` 整个目录（exe + `_internal\` + templates/static） |
| 数据（可选） | 一并拷贝 `data\` 目录（parquet 缓存 + duckdb），免重新同步 |
| 通达信定位 | 优先：通达信注册表卸载键匹配；否则设置环境变量 `TDX_INSTALL_DIR` |
| 通达信登录 | 先启动通达信并登录，再启动 StockQuant.exe |

```bat
@echo off
REM 启动脚本（通达信注册表定位失败时使用）
set TDX_INSTALL_DIR=D:\newtdx\通达信
set TDX_OPEN_BROWSER=1
start "" "%~dp0StockQuant.exe"
```

**环境变量覆盖**（config.py 全部支持）：

| 变量 | 默认 | 说明 |
|------|------|------|
| TDX_INSTALL_DIR | 注册表定位 | 通达信安装目录 |
| TDX_DATA_DIR / TDX_LOG_DIR | {exe 目录}/data、/logs | 数据/日志目录 |
| TDX_TQ_URL / TDX_TQ_TIMEOUT / TDX_TQ_MAX_RETRY | 127.0.0.1:17709 / 60 / 2 | TQ 服务 |
| HOST / PORT | 0.0.0.0 / 8765 | Flask 监听 |
| TDX_OPEN_BROWSER | 1 | 0=不自动开浏览器 |

### 11.5 运行验证

**注意**：EXE 冷启动约需 **20 秒**（通达信初始化 + Flask 加载），等待后再检查端口：

```powershell
# 启动
Start-Process "d:\AiPython\AIwork2\dist\StockQuant\StockQuant.exe"
# 等 20 秒后验证
Invoke-WebRequest http://127.0.0.1:8765/health   # {"ok":true}
Invoke-WebRequest "http://127.0.0.1:8765/api/kline?code=600519.SH&days=60"  # HTTP 200
# 日志
Get-Content "dist\StockQuant\logs\tdxlambda.log" -Tail 20
```

**启动日志关键行**：

```
通达信初始化完成 ✓
通达信行情连接正常 ✓          ← 数据源就绪
服务地址: http://127.0.0.1:8765
```

---

## 12. 二次开发指南

### 12.1 新增选股策略

在 stock_screener.py 继承 `BaseStrategy`，实现 `evaluate()` + `sort_key()` + `describe()`，注册到 `_STRATEGIES` 字典，前端 `/api/screener/strategies` 自动可见。

### 12.2 新增 TTLCache

```python
from cache import TTLCache, cache_bus
_my_cache = TTLCache(default_ttl=60, name="my_feature")
cache_bus.register(_my_cache, tags=["my_feature"])
_my_cache.get("key"); _my_cache.set("key", value)
cache_bus.invalidate_tags("my_feature")
```

### 12.3 新增基本面指标

fundamental_fields.py 的 FN_NAME / GP_NAME 追加，对应 TDX 接口把字段加进 table_list。EAV 模式天然兼容，不需 ALTER TABLE。

### 12.4 新增 API 端点（规范）

```python
@app.get("/api/example")
def api_example():
    code = request.args.get("code", "").strip()
    if not code:
        return jsonify({"error": "缺 code"}), 400
    try:
        return jsonify(some_service.do_something(code))
    except Exception as e:        # 一律 catch Exception
        return jsonify({"error": str(e)}), 500
```

### 12.5 新增基本面同步业务（规范）

```python
# 1. fundamental_service.py 增加 sync_xxx()（复用 fundamental_store upsert_*）
# 2. fundamental_store.py 建表（EAV 或宽表均可）
# 3. web_app.py 三处接入：
#    a. _DEFAULT_AUTO_SYNC["biz"] 增加开关 + _AUTO_SYNC_BIZ_LABELS 增加中文名
#    b. api_fundamental_sync() 的 _do_sync 分派加 elif
#    c. _background_data_sync() 的 biz 循环加 elif
```

### 12.6 接入自动同步任务管道

```python
tid = _tasks.submit(fn, progress=_progress_cb)
# fn 签名: fn(progress=None) → progress(stage, done, total, info)
# 前端: GET /api/screener/task/<tid>/stream → EventSource 监听
```

### 12.7 异常处理规范（强制）

所有 route 的 try/except 必须 `except Exception as e:`，禁止只 catch RuntimeError。Service 层可 raise 具体异常（RuntimeError=依赖缺失，ValueError=业务校验），route 层统一兜底。

### 12.8 常见陷阱速查

| 问题 | 原因 | 解决 | Phase |
|------|------|------|-------|
| `/api/kline` 返回 HTML 500 | `except RuntimeError` 漏 ValueError | 改 `except Exception` | 4 |
| `{"error":"'value'"}` | pivot_table 缺 value 列 | 加列名防御检查 | 4 |
| `Cannot access 'x' before initialization` | const 声明顺序 TDZ | 改 function 声明 | 4 |
| 选股页切换白屏 4s+ | 无缓存 | sessionStorage + TTLCache | 4 |
| 基本面长表筛选后下拉重置"全部" | renderLong 未保存选中状态 | 渲染前存值 + option selected | 5 |
| 重启后写事务"拒绝访问" | 退出未 CHECKPOINT 残留 .wal | atexit 优雅退出 | 5 |
| clear-all-cache 后重启读空文件 | 独立连接 + os.remove 孤儿句柄 | 进程内连接 DELETE + CHECKPOINT | 5 |
| **日线同步 saved=0 / errors=5553** | **TQ `get_market_data` 加 `_unwrap` 后返回格式变更，`_extract_code_df` 未兼容** | **`_extract_code_df` 兼容 unwrap/未 unwrap 两种格式** | **6** |
| **行业板块数据不显示** | **TQ `get_sector_list()` 返回 dict 列表，代码按 str 列表解析** | **`_normalize_codes` 归一化两种格式** | **6** |
| **K 线/预测/选股返回空 DataFrame** | **`if f in raw` 对 _unwrap 后结构失效** | **改 `sub.empty` 检查 + pd.to_numeric** | **6** |
| DuckDB 锁冲突 | 另一进程没关 | 任务管理器杀占用进程 | 3 |
| 局域网设备访问不到 | 防火墙没放行 8765 | netsh 加规则 | 1 |

---

## 13. 故障排查

### 13.1 回归测试结果（Phase 5/6）

| 测试项 | 结果 |
|--------|------|
| auto-sync config GET/POST 读写 + 原子保存 | ✅ |
| auto-sync/run 未启用 → 400 拒绝 | ✅ |
| 自动同步 SSE 进度流（progress/done/error） | ✅ |
| last_run / last_result 自动回写 | ✅ |
| 优雅退出三个 DuckDB 均 CHECKPOINT + close | ✅ |
| 重启后无 .wal 写事务异常 | ✅ |
| clear-all-cache 保留表结构 + 元表不动 | ✅ |
| 基本面长表报告期筛选状态保留 | ✅ |
| db-detail / db-delete 单票与全量删除 | ✅ |
| **日线全量同步 saved=5543 / errors=10 / ~104s** | ✅ |
| **四大功能（基本面/选股同步/首页四图/行业板块）** | ✅ |
| **EXE 打包运行：端口 8765 监听 + HTTP 200 + 图标嵌入** | ✅ |

### 13.2 常见错误速查

| 现象 | 根因 | 解决 |
|------|------|------|
| `/api/kline` 返回 HTML 500 | except RuntimeError 漏 ValueError | `except Exception as e` |
| `{"error":"'value'"}` | pivot_table 缺 value 列 | 已加列名防御；重启服务 |
| 重启后写事务"拒绝访问" | 残留 .wal | 已修复：优雅退出统一 CHECKPOINT + close |
| clear-all-cache 后库变空文件 | 独立连接 os.remove 孤儿句柄 | 已修复：进程内连接 DELETE + CHECKPOINT |
| 日线同步全失败 saved=0 | _extract_code_df 与 TQ 返回格式不匹配 | 已修复（★ Phase 6） |
| 行业板块空 | _normalize_codes 缺失 | 已修复（★ Phase 6） |
| EXE 启动后端口无监听 | **检查太早**（冷启动需 ~20s） | 等 20s 再验证 |
| `未找到通达信安装目录` | 注册表键不匹配 | 设置 TDX_INSTALL_DIR 环境变量 |
| `行情连接异常 (客户端未登录)` | 通达信未登录 | 先登录再启动 |
| `另一个程序正在使用此文件` | DuckDB 锁冲突 | 杀占用进程 |
| 局域网访问不到 | 防火墙 | netsh advfirewall 加 8765 规则 |

### 13.3 缓存清理安全策略

`POST /api/admin/clear-all-cache` 使用 **Flask 进程内部已持有的连接** 完成清理：

1. **Parquet**：物理删除（纯缓存，可安全重建）
2. **fundamental.duckdb**：`DELETE FROM 业务表` + `CHECKPOINT`
3. **market.duckdb**：DROP 各 code 表 + 清 kline_meta + `CHECKPOINT`
4. **不动**：field_meta / update_log / portfolio 表（保留表结构）

> 历史教训：旧实现用**独立新连接** `PRAGMA dropalltables` + `os.remove` 删除文件。Windows 下原连接句柄成为孤儿，checkpoint 写入无效，下次启动读到空文件 → 数据丢失。Phase 5 改为进程内连接清理。

### 13.4 调试工具

```bash
curl http://127.0.0.1:8765/api/cache/debug          # 缓存状态
curl http://127.0.0.1:8765/api/cache/freshness      # 数据新鲜度
curl -X POST http://127.0.0.1:8765/api/cache/invalidate ^
  -H "Content-Type: application/json" -d "{\"tags\":[\"kline\"]}"
curl http://127.0.0.1:8765/api/admin/flow           # 数据流拓扑
curl http://127.0.0.1:8765/api/admin/tq-test        # TDX 连通测试
curl http://127.0.0.1:8765/api/fundamental/auto-sync/config
```

---

## 附录 A：API 字段名速查

| 前缀 | 数量 | 分类 |
|------|------|------|
| FN1 ~ FN438 | 438 | 财务报表（FN_NAME） |
| GP01 ~ GP46 | 46 | GP 交易指标（GP_NAME） |
| MCST, CYS, ASR, SCR, CYC1~CYC5 | 9 | 筹码 |
| CJBS, BOrder | N | L2 |
| 主营构成 | 按产品/行业/地区 | mainbusi_facts（含收入/成本/毛利） |

## 附录 B：版本历史

| 版本 | 日期 | 主要变更 |
|------|------|----------|
| v1.0 | 2026-08-10 | Phase 1：行情四图 + 交易 + 预测 + 行业 + admin |
| v2.0 | 2026-08-11 | Phase 2：基本面（438+46+筹码+股东）+ 前后端分离 + DuckDB EAV |
| v3.0 | 2026-08-13 | Phase 3：选股引擎 + config.py + logger.py |
| v4.0 | 2026-08-14 | Phase 4：cache.py + Service 分层 + 三级 TTL + 并行 + 秒显 + 异常加固 + 列名防御 |
| **v5.0** | **2026-08-15** | **Phase 5：run.py 启动入口 + 自动同步 + 后台任务 + 管理增强 + 优雅退出 + 状态保留** |
| **v6.0** | **2026-08-15** | **Phase 6：TQ 格式兼容加固（_unwrap/sub.empty/_extract_code_df/_normalize_codes）+ 日线同步 2 倍提速 + PyInstaller 打包 + 图标 + EXE 验证 + 交付清理** |

## 附录 C：Phase 5/6 变更清单

| 文件 | 操作 | 说明 |
|------|------|------|
| run.py | **新增 (P5)** | 独立启动入口：目录初始化 + 自动开浏览器 + app.run |
| web_app.py | **修改** | auto_sync 配置管理 + 3 个 auto-sync 路由 + _TaskManager/SSE + 优雅退出 + 安全清缓存 + db-detail/db-delete/clear-memory + _fix_cwd_for_exe + flow 文案校准 |
| fundamental_store.py | **修改** | 新增 mainbusi_profile 表 |
| fundamental_service.py | **修改** | sync_mainbusi/shareholder/mv 完善；批量 upsert 优化 |
| tdx_tq_client.py | **修改 (P6)** | 数据接口统一 `_unwrap`；`price_df` 兼容三种格式 |
| stock_screener.py | **修改 (P6)** | `_extract_code_df` 兼容两种格式（日线同步全量修复） |
| quote_service.py | **修改 (P6)** | `_normalize_codes` 归一化 dict/str 板块列表 |
| kline_service.py / stock_app.py / predict_service.py | **修改 (P6)** | `sub.empty` 防御 + pd.to_numeric |
| fundamental.js | **修改 (P5)** | renderLong 报告期下拉状态保留 |
| admin.js | **修改 (P5)** | DuckDB 表卡片副标题文案校准 |
| StockQuant.spec | **新增 (P6)** | PyInstaller onedir 打包配置（icon + datas + excludes） |
| app.ico | **新增 (P6)** | 程序图标（JPG 源图转换，7 尺寸） |
| auto_sync.json | **新增 (P5)** | 自动同步运行时配置（首次启动自动生成） |
| batch_sync.py | **删除 (P5)** | 由 _background_data_sync + 自动同步机制替代 |
