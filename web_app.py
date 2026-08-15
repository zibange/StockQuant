# Last modified: 2026-08-13 01:30:56
"""
股票量化可视化 —— Flask + ECharts
运行: python web_app.py
访问: http://127.0.0.1:8765
依赖: pip install flask duckdb pyarrow pandas
"""
import os, sys, json, platform, threading, uuid, queue
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
from datetime import datetime
from flask import Flask, request, jsonify, render_template, session, Response
import pandas as pd
import os, hashlib, secrets

from stock_app import _init_tdx, KlineStore, PortfolioStore
from tdx_tq_client import TQLocalClient
from fundamental_service import FundamentalService
from fundamental_fields import FN_NAME
from stock_screener import StockScreener, MV_BUCKETS, default_allowed_buckets, list_strategies
from predict_service import PredictService
from quote_service import QuoteService
from kline_service import KlineService
from logger import get_logger
from cache import TTLCache, cache_bus, all_cache_info
import config as _cfg
_log = get_logger("web_app")

# ===============================================================
# 自动同步配置管理 (auto_sync.json)
# ===============================================================
_AUTO_SYNC_PATH = os.path.join(str(_cfg.DATA_DIR), "auto_sync.json")

_DEFAULT_AUTO_SYNC = {
    "enabled": False,
    "biz": {
        "stock_basic": False,
        "financial": False,
        "gpjy": False,
        "chip": False,
        "l2": False,
        "shareholder": False,
        "mainbusi": False,
    },
    "on_startup": False,
    "delay_seconds": 3,
    "last_run": None,
    "last_result": None,
}

_AUTO_SYNC_BIZ_LABELS = {
    "stock_basic": "基础信息",
    "financial": "专业财务",
    "gpjy": "交易专业数据",
    "chip": "筹码指标",
    "l2": "L2扩展",
    "shareholder": "股东明细",
    "mainbusi": "主营构成",
}


def _load_auto_sync_config():
    if not os.path.exists(_AUTO_SYNC_PATH):
        _save_auto_sync_config(_DEFAULT_AUTO_SYNC)
        return dict(_DEFAULT_AUTO_SYNC)
    try:
        with open(_AUTO_SYNC_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        merged = dict(_DEFAULT_AUTO_SYNC)
        merged.update(cfg)
        merged["biz"] = dict(_DEFAULT_AUTO_SYNC["biz"])
        merged["biz"].update(cfg.get("biz", {}))
        return merged
    except Exception:
        return dict(_DEFAULT_AUTO_SYNC)


def _save_auto_sync_config(cfg):
    try:
        tmp = _AUTO_SYNC_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        os.replace(tmp, _AUTO_SYNC_PATH)
    except Exception as e:
        _log.error("保存 auto_sync.json 失败: %s", e)


def _auto_sync_get_enabled_biz(cfg):
    biz_map = cfg.get("biz", {})
    return [b for b, on in biz_map.items() if on]


_auto_sync_config = _load_auto_sync_config()

app = Flask(__name__)
app.config["JSON_AS_ASCII"] = False
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY") or secrets.token_hex(16)
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_PERMANENT"] = True

import time as _time

# ===============================================================
# 后台任务管理器 (SSE 进度推送)
# ===============================================================
class _TaskManager:
    def __init__(self):
        self._tasks = {}  # task_id -> dict
        self._lock = threading.Lock()

    def submit(self, fn, *args, **kwargs):
        """提交后台任务, 返回 task_id"""
        tid = uuid.uuid4().hex[:12]
        q = queue.Queue()
        task = {
            "id": tid,
            "status": "running",
            "progress": 0,
            "total": 100,
            "stage": "",
            "msg": "",
            "result": None,
            "error": None,
            "queue": q,
            "subscribers": [],  # 活跃的 SSE 客户端
            "created_at": _time.time(),
        }
        with self._lock:
            self._tasks[tid] = task

        def _worker():
            def _progress_cb(stage, done, total, info):
                pct = 0
                if isinstance(total, int) and total > 0 and isinstance(done, int):
                    pct = min(100, int(done / total * 100))
                task["progress"] = pct
                task["stage"] = stage
                task["msg"] = info.get("msg", "")
                info_copy = dict(info)
                info_copy.setdefault("stage", stage)
                info_copy["_pct"] = pct
                try:
                    q.put_nowait({"type": "progress", "pct": pct,
                                  "stage": stage, "done": done, "total": total, "info": info_copy})
                except queue.Full:
                    pass

            try:
                result = fn(*args, progress=_progress_cb, **kwargs)
                task["status"] = "done"
                task["progress"] = 100
                task["result"] = result
                q.put({"type": "done", "result": result})
                on_done = task.get("_on_done")
                if on_done:
                    try:
                        on_done(result)
                    except Exception:
                        _log.error("task %s on_done fail", tid, exc_info=True)
            except Exception as e:
                task["status"] = "error"
                task["error"] = str(e)
                _log.error("task %s fail: %s", tid, e, exc_info=True)
                q.put({"type": "error", "error": str(e)})

        threading.Thread(target=_worker, daemon=True, name=f"task-{tid}").start()
        return tid

    def get(self, tid):
        with self._lock:
            t = self._tasks.get(tid)
            if not t:
                return None
            return {k: v for k, v in t.items() if k not in ("queue", "subscribers")}

    def stream(self, tid):
        """生成 SSE 事件流 (返回 bytes, 供 Werkzeug direct_passthrough 使用)"""
        def _enc(s): return s.encode("utf-8")
        with self._lock:
            t = self._tasks.get(tid)
            if not t:
                yield _enc("event: error\ndata: " + json.dumps({"type": "error", "error": "task not found"}) + "\n\n")
                return
            q = t["queue"]

        ended = False
        while True:
            try:
                evt = q.get(timeout=30)
                etype = evt.get("type", "message")
                yield _enc("event: " + etype + "\ndata: " + json.dumps(evt, ensure_ascii=False) + "\n\n")
                if etype in ("done", "error"):
                    ended = True
                    break
            except queue.Empty:
                if ended:
                    break
                yield _enc(": keep-alive\n\n")

        import time as _t
        for _ in range(3):
            _t.sleep(0.3)
            yield _enc(": keep-alive\n\n")

    def cleanup_old(self, max_age=600):
        """清理 10 分钟前的已完成任务"""
        now = _time.time()
        with self._lock:
            dead = [tid for tid, t in self._tasks.items()
                    if t["status"] in ("done", "error") and now - t["created_at"] > max_age]
            for tid in dead:
                self._tasks.pop(tid, None)


_tasks = _TaskManager()


@app.before_request
def _log_request_start():
    _r = request
    _r._log_t0 = _time.perf_counter()
    if _r.path.startswith("/api/"):
        _log.info("→ %s %s", _r.method, _r.full_path)

@app.teardown_request
def _log_request_end(_exc):
    _r = request
    if not hasattr(_r, "_log_t0"):
        return
    _dt = (_time.perf_counter() - _r._log_t0) * 1000
    if _r.path.startswith("/api/"):
        _log.debug("← %s %s  %.0fms", _r.method, _r.full_path, _dt)
import datetime as _dt
_START_TIME = _dt.datetime.now()

# Admin endpoint toggle
_api_enabled = {}

_AUTH_OPEN = {"/", "/api/auth/login", "/api/auth/register",
              "/api/auth/me", "/api/version", "/api/search",
              "/api/kline", "/api/predict", "/api/quote", "/api/sector",
              "/api/admin/status", "/api/admin/db-detail",
              "/static/"}


def _current_user():
    uid = session.get("uid")
    if not uid:
        return None
    return _portfolio.get_user(uid)


def _require_user():
    user = _current_user()
    if not user:
        return None, (jsonify({"error": "未登录", "login_required": True}), 401)
    return user, None


@app.before_request
def _admin_gate():
    from flask import request as _r
    _p = _r.path
    if _p.startswith("/api/") and _p in _api_enabled and not _api_enabled[_p]:
        from flask import make_response
        resp = make_response(jsonify({"error": "endpoint_disabled", "path": _p}), 503)
        resp.headers["X-Admin-Disabled"] = "1"
        return resp
    # admin 路径统一鉴权 (除了 status/flow 等只读概要)
    if _p.startswith("/api/admin/"):
        if _p in ("/api/admin/status", "/api/admin/flow"):
            return None
        if _current_user() is None:
            return jsonify({"error": "未登录, 需要管理员权限", "login_required": True}), 401
    if _p.startswith("/api/portfolio") or _p.startswith("/api/watchlist"):
        if _current_user() is None:
            if _p not in _AUTH_OPEN:
                return jsonify({"error": "未登录", "login_required": True}), 401
import datetime as _dt
_START_TIME = _dt.datetime.now()

_admin_status_cache = {"ts": 0, "data": None, "elapsed_ms": 0}
_ADMIN_STATUS_TTL = 15

print("初始化通达信 TQ-Python...", flush=True)
_log.info("初始化通达信 TQ-Python...")
try:
    _init_tdx()  # 启动 tqcenter HTTP JSON-RPC 服务
    _tq = TQLocalClient()   # 所有 service 统一走 HTTP 客户端 (避免原生 SDK 返回格式不兼容)
    print("通达信初始化完成 ✓", flush=True)
    _log.info("通达信初始化完成 ✓")
    try:
        _tq.get_market_snapshot(stock_code="002415.SZ")
        print("通达信行情连接正常 ✓", flush=True)
        _log.info("通达信行情连接正常 ✓")
    except Exception as _e:
        print(f"[警告] 行情连接异常 (通达信客户端可能未登录): {_e}", flush=True)
        _log.warning("行情连接异常 (通达信客户端可能未登录): %s", _e)
except Exception as _e:
    print(f"[错误] 通达信初始化失败: {_e}", flush=True)
    print("       请确认已安装通达信客户端并已启动，然后重新运行。", flush=True)
    _log.error("通达信初始化失败: %s — 请确认已安装通达信客户端并已启动", _e)
    _tq = None

_kline_store = KlineStore(str(_cfg.DATA_DIR))
_portfolio = PortfolioStore(str(_cfg.DATA_DIR / "portfolio.duckdb"), init_cash=1_000_000)
_fundamental = FundamentalService(str(_cfg.DATA_DIR))
_screener = StockScreener(str(_cfg.DATA_DIR), tq_client=_tq, fm_service=_fundamental, kl_store=_kline_store)
_predict = PredictService(kl_store=_kline_store, tq_client=_tq, portfolio=_portfolio)
_quote = QuoteService(tq_client=_tq)
_kline_svc = KlineService(kl_store=_kline_store, tq_client=_tq)


_route_cache = TTLCache(default_ttl=30, name="route")
cache_bus.register(_route_cache, tags=["screener.status", "screener.buckets", "cache.freshness"])

# ======================== API ========================

@app.get("/api/version")
def api_version():
    return jsonify({"version": "2.0.0", "python": platform.python_version(), "os": platform.system()})


# ======================== 基本面数据 (功能1: 数据处理) ========================

def _parse_page(request, default_size=10):
    """解析分页参数: page(>=1), page_size ∈ {10,30,50,100}"""
    try:
        page = max(int(request.args.get("page", "1")), 1)
    except ValueError:
        page = 1
    try:
        ps = int(request.args.get("page_size", str(default_size)))
    except ValueError:
        ps = default_size
    if ps not in (10, 30, 50, 100):
        ps = default_size
    return page, ps


def _rows_json(df):
    """DataFrame 行安全转 JSON 列表: NaN→null (to_dict 会输出 NaN,
    浏览器 JSON.parse 拒绝, 引发前端 fetch 静默失败)"""
    if df is None or df.empty:
        return []
    return json.loads(df.to_json(orient="records", force_ascii=False))


def _clean_nan(obj):
    """递归清理 dict/list 中的 float('nan') → None, 防止 Flask jsonify 输出
    浏览器无法解析的字面量 NaN"""
    import math
    if isinstance(obj, dict):
        return {k: _clean_nan(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_clean_nan(v) for v in obj]
    if isinstance(obj, float) and math.isnan(obj):
        return None
    return obj


@app.get("/api/fundamental/profile")
def api_fundamental_profile():
    """单票基本面画像: 基础信息 + 估值 + 财务宽表"""
    code = (request.args.get("code") or "").strip().upper()
    if not code:
        return jsonify({"error": "缺 code"}), 400
    prof = _fundamental.get_profile(code)
    return jsonify(_clean_nan(prof))


@app.get("/api/fundamental/financial")
def api_fundamental_financial():
    """财务数据查询: format=wide|long, report_date 可选, fields 可选
    分页: page(从1开始), page_size(10/30/50/100, 默认10)
    """
    code = (request.args.get("code") or "").strip().upper()
    if not code:
        return jsonify({"error": "缺 code"}), 400
    fmt = request.args.get("format", "wide")
    report_date = request.args.get("report_date") or None
    fields = request.args.get("fields") or None
    if fields:
        fields = [f.strip() for f in fields.split(",") if f.strip()]
    page, page_size = _parse_page(request)
    # 长表报告期下拉需全量日期列表 (轻量)
    if request.args.get("all_dates") == "1":
        dates = _fundamental.store.list_financial_dates(code)
        return jsonify({"code": code, "format": "long",
                        "dates": [str(x) for x in dates.get("report_date", [])],
                        "total": 0, "page": 1, "page_size": 0})
    try:
        if fmt == "long":
            df = _fundamental.get_financial_long(code, report_date)
            total = len(df)
            if total:
                df = df.sort_values(["report_date", "field_code"],
                                    ascending=False)
                df = df.iloc[(page - 1) * page_size: page * page_size]
            return jsonify({"code": code, "format": "long",
                            "rows": _rows_json(df),
                            "total": total, "page": page,
                            "page_size": page_size})
        df = _fundamental.get_financial_wide(code, report_date, fields)
        total = len(df)
        if total:
            df = df.sort_values("report_date", ascending=False)
            df = df.iloc[(page - 1) * page_size: page * page_size]
        return jsonify({"code": code, "format": "wide",
                        "rows": _rows_json(df),
                        "total": total, "page": page, "page_size": page_size})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.post("/api/fundamental/sync")
def api_fundamental_sync():
    """触发同步: body {codes:[...], biz:"basic"|"financial"|"gpjy"|"chip"|"l2"|"shareholder"|"mainbusi", async:bool}

    async=true 时返回 task_id, 前端通过 /api/screener/task/<tid>/stream 接收 SSE 进度
    chip: 筹码指标 (MCST/CYS/ASR/SCR/CYC), days 回看天数
    l2: L2 扩展日线增量积累, count 拉取近期条数
    shareholder: 十大股东/十大流通股东明细, years 回看年数
    mainbusi: 主营构成明细, years 回看年数
    """
    data = request.get_json(force=True, silent=True) or {}
    codes = [c.strip().upper() for c in (data.get("codes") or []) if c.strip()]
    biz = data.get("biz", "basic")
    async_mode = bool(data.get("async"))
    if not codes:
        return jsonify({"error": "需提供 codes 列表"}), 400

    def _do_sync(progress=None):
        if biz == "financial":
            return _fundamental.sync_financial(
                codes, report_type=data.get("report_type", "report_time"),
                start_time=data.get("start_time"), end_time=data.get("end_time"),
                progress=progress)
        elif biz == "gpjy":
            return _fundamental.sync_gpjy(
                codes, start_time=data.get("start_time"),
                end_time=data.get("end_time"), progress=progress)
        elif biz == "chip":
            return _fundamental.sync_chip(
                codes, days=int(data.get("days", 250)), progress=progress)
        elif biz == "l2":
            return _fundamental.sync_l2(
                codes, count=int(data.get("count", 60)), progress=progress)
        elif biz == "shareholder":
            return _fundamental.sync_shareholder(
                codes, years=int(data.get("years", _fundamental.SHAREHOLDER_YEARS)),
                force=bool(data.get("force")), progress=progress)
        elif biz == "mainbusi":
            return _fundamental.sync_mainbusi(
                codes, years=int(data.get("years", _fundamental.MAINBUSI_YEARS)),
                force=bool(data.get("force")), progress=progress)
        else:
            return _fundamental.sync_stock_basic(
                codes, force=bool(data.get("force")), progress=progress)

    if async_mode:
        tid = _tasks.submit(_do_sync)
        return jsonify({"ok": True, "task_id": tid, "biz": biz,
                        "stream_url": f"/api/screener/task/{tid}/stream"})
    else:
        r = _do_sync()
        return jsonify({"biz": biz, "codes": codes, **r})


@app.get("/api/fundamental/auto-sync/config")
def api_auto_sync_get_config():
    """获取自动同步配置 (含 last_run / last_result)"""
    return jsonify(dict(_auto_sync_config))


@app.post("/api/fundamental/auto-sync/config")
def api_auto_sync_save_config():
    """保存自动同步配置"""
    global _auto_sync_config
    data = request.get_json(force=True, silent=True) or {}

    if "enabled" in data:
        _auto_sync_config["enabled"] = bool(data["enabled"])
    if "on_startup" in data:
        _auto_sync_config["on_startup"] = bool(data["on_startup"])
    if "delay_seconds" in data:
        try:
            ds = int(data["delay_seconds"])
            _auto_sync_config["delay_seconds"] = max(0, min(3600, ds))
        except (ValueError, TypeError):
            pass
    if "biz" in data and isinstance(data["biz"], dict):
        for b in _auto_sync_config["biz"]:
            if b in data["biz"]:
                _auto_sync_config["biz"][b] = bool(data["biz"][b])

    _save_auto_sync_config(_auto_sync_config)
    return jsonify({"ok": True, "config": dict(_auto_sync_config)})


@app.post("/api/fundamental/auto-sync/run")
def api_auto_sync_run_now():
    """手动触发一次自动同步 (走 SSE 进度流)"""
    global _auto_sync_config
    if not _auto_sync_config.get("enabled"):
        return jsonify({"ok": False, "error": "自动同步未启用"}), 400

    biz_list = _auto_sync_get_enabled_biz(_auto_sync_config)
    if not biz_list:
        return jsonify({"ok": False, "error": "未选择任何同步业务"}), 400

    def _do_sync(progress=None):
        return _background_data_sync(codes=None, biz_list=biz_list,
                                     progress=progress)

    def _on_done(result):
        _auto_sync_config["last_run"] = datetime.now().strftime(
            "%Y-%m-%d %H:%M:%S")
        _auto_sync_config["last_result"] = {
            biz: {k: v for k, v in r.items() if k != "per_code"}
            for biz, r in result.items()
        }
        _save_auto_sync_config(_auto_sync_config)

    tid = _tasks.submit(_do_sync)
    _tasks._tasks[tid]["_on_done"] = _on_done
    return jsonify({"ok": True, "task_id": tid,
                    "biz": biz_list,
                    "stream_url": f"/api/screener/task/{tid}/stream"})


@app.get("/api/fundamental/summary")
def api_fundamental_summary():
    """基本面库概览 (管理端展示)"""
    summ = _fundamental.get_summary()
    logs = _fundamental.store.recent_logs(10)
    log_rows = []
    for _, r in logs.iterrows():
        log_rows.append({c: (str(r[c]) if r[c] is not None else "") for c in logs.columns})
    return jsonify({"tables": summ, "recent_logs": log_rows})


@app.get("/api/fundamental/fields")
def api_fundamental_fields():
    """字段元数据: category=financial 返回全部 FN 字段中文名
    分页: page, page_size; 传 all=1 时一次返回全部 (轻量元数据, 供字段芯片)
    """
    page, page_size = _parse_page(request, default_size=100)
    if request.args.get("all") == "1":
        df = _fundamental.store.con.execute(
            "SELECT field_code, field_name, category, source_api "
            "FROM field_meta ORDER BY field_code").df()
        return jsonify({"fields": df.to_dict(orient="records"),
                        "total": len(df), "page": 1,
                        "page_size": len(df)})
    total = _fundamental.store.con.execute(
        "SELECT COUNT(*) FROM field_meta").fetchone()[0]
    df = _fundamental.store.con.execute(
        "SELECT field_code, field_name, category, source_api "
        "FROM field_meta ORDER BY field_code "
        "LIMIT ? OFFSET ?",
        [page_size, (page - 1) * page_size]).df()
    return jsonify({"fields": df.to_dict(orient="records"),
                    "total": total, "page": page, "page_size": page_size})


@app.get("/api/fundamental/gpjy")
def api_fundamental_gpjy():
    """GP 交易专业数据查询: code 必填, 返回 trade_date x GP字段 宽表
    分页: page(从1开始), page_size(10/30/50/100, 默认10)
    """
    code = (request.args.get("code") or "").strip().upper()
    if not code:
        return jsonify({"error": "缺 code"}), 400
    fields = request.args.get("fields") or None
    page, page_size = _parse_page(request)
    sql = "SELECT * FROM gpjy_facts WHERE code=?"
    args = [code]
    if fields:
        fl = [f.strip() for f in fields.split(",") if f.strip()]
        if fl:
            placeholders = ",".join("?" * len(fl))
            sql += f" AND field_code IN ({placeholders})"
            args += fl
    df = _fundamental.store.con.execute(sql, args).df()
    if df.empty:
        return jsonify({"code": code, "rows": [], "dates": [], "fields": [],
                        "total": 0, "page": page, "page_size": page_size})
    wide = df.pivot_table(index="trade_date", columns="field_code",
                          values="value", aggfunc="first").reset_index()
    wide.columns = [str(c) for c in wide.columns]
    wide = wide.sort_values("trade_date", ascending=False)
    total = len(wide)
    wide_page = wide.iloc[(page - 1) * page_size: page * page_size]
    dates = [str(x) for x in wide_page["trade_date"]]
    gp_fields = [c for c in wide.columns if c != "trade_date"]
    return jsonify({"code": code, "rows": _rows_json(wide_page),
                    "dates": dates, "fields": gp_fields,
                    "total": total, "page": page, "page_size": page_size})


@app.get("/api/fundamental/chip")
def api_fundamental_chip():
    """筹码指标查询 (MCST/CYS/ASR/SCR/CYC): code 必填, 返回 trade_date x 指标宽表"""
    code = (request.args.get("code") or "").strip().upper()
    if not code:
        return jsonify({"error": "缺 code"}), 400
    fields = request.args.get("fields") or None
    limit = min(int(request.args.get("limit", "300")), 2000)
    df = _fundamental.get_chip(code, fields, limit)
    if df.empty:
        return jsonify({"code": code, "rows": [], "fields": [], "total": 0})
    cols = [c for c in df.columns if c != "trade_date"]
    return jsonify({"code": code,
                    "rows": _rows_json(df),
                    "fields": cols, "total": len(df)})


@app.get("/api/fundamental/l2")
def api_fundamental_l2():
    """L2 扩展日线查询: code 必填"""
    code = (request.args.get("code") or "").strip().upper()
    if not code:
        return jsonify({"error": "缺 code"}), 400
    fields = request.args.get("fields") or None
    limit = min(int(request.args.get("limit", "300")), 2000)
    df = _fundamental.get_l2(code, fields, limit)
    if df.empty:
        return jsonify({"code": code, "rows": [], "fields": [], "total": 0})
    cols = [c for c in df.columns if c != "trade_date"]
    return jsonify({"code": code,
                    "rows": df.to_dict(orient="records"),
                    "fields": cols, "total": len(df)})


@app.get("/api/fundamental/shareholder")
def api_fundamental_shareholder():
    """十大股东/十大流通股东明细查询: code 必填
    holder_type: gd=十大股东 / ltgd=十大流通股东 (缺省返回两者)
    report_date: 指定报告期 (缺省返回全部报告期)
    分页: page(从1开始), page_size(10/30/50/100, 默认10)
    """
    code = (request.args.get("code") or "").strip().upper()
    if not code:
        return jsonify({"error": "缺 code"}), 400
    holder_type = (request.args.get("holder_type") or "").strip() or None
    if holder_type not in (None, "gd", "ltgd"):
        return jsonify({"error": "holder_type 仅支持 gd/ltgd"}), 400
    report_date = (request.args.get("report_date") or "").strip() or None
    page, page_size = _parse_page(request)
    df = _fundamental.get_shareholder(code, holder_type, report_date)
    total = len(df)
    if df.empty:
        return jsonify({"code": code, "rows": [], "dates": [],
                        "total": 0, "page": page, "page_size": page_size})
    df_page = df.iloc[(page - 1) * page_size: page * page_size]
    cols = [c for c in df.columns
            if c not in ("code", "updated_at")]
    dates = _fundamental.get_shareholder_dates(code)
    return jsonify({"code": code,
                    "rows": _rows_json(df_page),
                    "columns": cols, "dates": dates,
                    "total": total, "page": page, "page_size": page_size})


@app.get("/api/fundamental/mainbusi")
def api_fundamental_mainbusi():
    """主营构成明细查询: code 必填
    report_date: 指定报告期 (缺省返回全部报告期)
    返回 rows+columns+dates+profile(概述)
    """
    code = (request.args.get("code") or "").strip().upper()
    if not code:
        return jsonify({"error": "缺 code"}), 400
    report_date = (request.args.get("report_date") or "").strip() or None
    df = _fundamental.get_mainbusi(code, report_date)
    total = len(df)
    if df.empty:
        return jsonify({"code": code, "rows": [], "columns": [],
                        "dates": [], "profile": None,
                        "total": 0})
    # df.to_json 将 NaN 序列化为 null (浏览器 JSON.parse 拒绝 NaN)
    rows = json.loads(df.to_json(orient="records", force_ascii=False))
    cols = [c for c in df.columns if c not in ("code", "updated_at")]
    dates = _fundamental.get_mainbusi_dates(code)
    profile = _fundamental.get_mainbusi_profile(code)
    return jsonify({"code": code,
                    "rows": rows,
                    "columns": cols, "dates": dates,
                    "profile": profile, "total": total})


@app.get("/api/fundamental/trace")
def api_fundamental_trace():
    """溯源视图: 库表结构 + 数据来源接口 + 更新日志 + 已同步股票"""
    summ = _fundamental.get_summary()
    logs = _fundamental.store.recent_logs(30)
    log_rows = []
    for _, r in logs.iterrows():
        log_rows.append({c: (str(r[c]) if r[c] is not None else "") for c in logs.columns})
    # 已同步股票清单
    codes = []
    for tbl in ("stock_info", "stock_more"):
        try:
            df = _fundamental.store.con.execute(
                f"SELECT code FROM {tbl} ORDER BY code").df()
            codes.extend(df["code"].tolist())
        except Exception:
            pass
    codes = sorted(set(codes))
    # 表结构
    tables = []
    for tbl in ("stock_info", "stock_more", "financial_facts",
                "gpjy_facts", "chip_facts", "l2_facts",
                "shareholder_facts", "mainbusi_facts", "mainbusi_profile",
                "field_meta", "update_log"):
        try:
            info = _fundamental.store.con.execute(
                "SELECT COUNT(*) FROM information_schema.columns "
                "WHERE table_name=?", [tbl]).fetchone()[0]
            rows = _fundamental.store.con.execute(
                f'SELECT COUNT(*) FROM "{tbl}"').fetchone()[0]
            tables.append({"name": tbl, "columns": info, "rows": rows})
        except Exception:
            tables.append({"name": tbl, "columns": 0, "rows": 0})
    return jsonify({
        "tables": tables,
        "summary": summ,
        "recent_logs": log_rows,
        "codes": codes,
        "data_flow": [
            {"step": "通达信客户端", "detail": "TdxW.exe 本地行情/财务引擎", "iface": "-"},
            {"step": "TQLocalClient", "detail": "HTTP JSON-RPC @127.0.0.1:17709", "iface": "get_stock_info/get_more_info/get_financial_data/get_gpjy_value/formula_process_mul_zb/download_file/read_mainbusi_file"},
            {"step": "FundamentalStore", "detail": "data/fundamental.duckdb 长表入库", "iface": "stock_info/stock_more/financial_facts/gpjy_facts/chip_facts/shareholder_facts/mainbusi_facts/mainbusi_profile"},
            {"step": "API", "detail": "Flask /api/fundamental/*", "iface": "profile/financial/gpjy/summary/mainbusi/trace"},
            {"step": "前端页面", "detail": "/fundamental 数据展示与溯源", "iface": "ECharts + Fetch"},
        ],
    })


@app.get("/api/fundamental/synced")
def api_fundamental_synced():
    """已同步股票清单 (轻量分页): code + name + 六大类型同步状态

    页面左侧"已同步股票"专用, 避免进入页面即拉取全量溯源数据。
    六大类型: basic/financial/gpjy/chip/l2/shareholder
    """
    page = max(1, int(request.args.get("page", 1)))
    page_size = min(50, max(1, int(request.args.get("page_size", 10))))
    return jsonify(_fundamental.store.list_synced(page=page, page_size=page_size))


# ======================== 选股引擎 ========================
@app.get("/api/screener/buckets")
def api_screener_buckets():
    cached = _route_cache.get("scr_buckets")
    if cached:
        return jsonify(cached)
    buckets_status = _screener.get_mv_cache_status().get("buckets", {})
    buckets_out = []
    for b in MV_BUCKETS:
        count = buckets_status.get(b["id"], 0)
        buckets_out.append({
            "id": b["id"],
            "name": b["name"],
            "lo": b["lo"],
            "hi": b["hi"] if b["hi"] < 1e11 else None,
            "hi_label": "∞" if b["hi"] >= 1e11 else int(b["hi"]),
            "cached_count": count,
            "excluded_by_default": b["exclude"],
        })
    resp = {
        "buckets": buckets_out,
        "default_allowed": default_allowed_buckets(),
        "description": "6档市值分桶: C1微盘(<20亿,默认排除) / C2小盘(20-50) / C3中盘(50-100) / C4中大盘(100-200) / C5大盘(200-1000) / C6超大盘(1000+)",
    }
    _route_cache.set("scr_buckets", resp)
    return jsonify(resp)


@app.get("/api/screener/strategies")
def api_screener_strategies():
    """可用选股策略列表 (元数据 + 默认策略)"""
    return jsonify({
        "ok": True,
        "default": "kdj",
        "strategies": list_strategies(),
    })


@app.post("/api/screener/sync")
def api_screener_sync():
    """批量同步市值 + 日线缓存 (异步, 返回 task_id)

    body:
      biz:       "all" | "mv" | "kline" (默认 all)
      codes:     [code1, code2, ...] 可选, 不填=全A股
      force:     bool, 无视缓存全量刷新 (默认 False)
      kline_count: int, 日线天数 (默认 60)
    """
    data = request.get_json(force=True, silent=True) or {}
    biz = data.get("biz", "all")
    codes = data.get("codes")
    force = bool(data.get("force"))
    kline_count = int(data.get("kline_count", 60))

    if _tq is None:
        return jsonify({"ok": False, "error": "通达信未初始化"}), 503

    if biz == "all":
        tid = _tasks.submit(_screener.sync_all, force=force, kline_count=kline_count)
    elif biz == "mv":
        tid = _tasks.submit(_screener.sync_mv, codes=codes, force=force)
    elif biz == "kline":
        tid = _tasks.submit(_screener.sync_kline, codes=codes, count=kline_count, force=force)
    else:
        return jsonify({"ok": False, "error": f"unknown biz: {biz}"}), 400

    return jsonify({"ok": True, "task_id": tid, "biz": biz})


@app.get("/api/screener/task/<tid>/stream")
def api_screener_task_stream(tid):
    """SSE 进度流"""
    resp = Response(_tasks.stream(tid), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache, no-transform",
                             "X-Accel-Buffering": "no",
                             "Connection": "keep-alive"})
    resp.direct_passthrough = True
    return resp


@app.get("/api/screener/task/<tid>")
def api_screener_task_status(tid):
    """轮询任务状态 (备用)"""
    info = _tasks.get(tid)
    if info is None:
        return jsonify({"ok": False, "error": "task not found"}), 404
    return jsonify({"ok": True, **info})


@app.get("/api/cache/freshness")
def api_cache_freshness():
    """缓存新鲜度 (A+B 双校验) — TTL=30s 缓存"""
    cached = _route_cache.get("freshness")
    if cached is not None:
        return jsonify(cached)
    try:
        result = _screener.check_cache_freshness()
        out = {"ok": True, "freshness": result}
        _route_cache.set("freshness", out)
        return jsonify(out)
    except Exception as e:
        _log.error("cache freshness check fail: %s", e, exc_info=True)
        return jsonify({"ok": False, "error": str(e)}), 500


@app.get("/api/cache/debug")
def api_cache_debug():
    """缓存观测: 所有 TTLCache 实例的命中率 / 大小 / 过期倒计时"""
    caches = all_cache_info()
    return jsonify({
        "ok": True,
        "total": len(caches),
        "tags": cache_bus.all_tags(),
        "caches": caches,
    })


@app.post("/api/cache/invalidate")
def api_cache_invalidate():
    """按 tag 手动失效缓存 (debug 用)

    body: {"tags": ["kline", "quote.sector"]}  或  {"all": true}
    """
    body = request.get_json(force=True) or {}
    if body.get("all"):
        n = cache_bus.invalidate_tags(*cache_bus.all_tags())
        return jsonify({"ok": True, "cleared": n, "mode": "all"})
    tags = body.get("tags") or []
    if not tags:
        return jsonify({"ok": False, "error": "需要 tags 数组 或 all=true"}), 400
    n = cache_bus.invalidate_tags(*tags)
    return jsonify({"ok": True, "cleared": n, "tags": tags})


@app.get("/api/screener/pick")
def api_screener_pick():
    """执行选股

    query params:
      strategy:        str, 策略名 (默认 "kdj", 可用 /api/screener/strategies 查看)
      top_n:           int, 输出前N名 (默认 10)
      buckets:         str, 逗号分隔的允许桶, 如 "C2,C3,C4,C5,C6" (默认排除C1)
      exclude_st:      bool, 排除ST/退市 (默认 true)
      min_amount_wan:  int, 近5日日均成交额下限万元 (默认 500)
      min_list_days:   int, 上市天数下限 (默认 60)
      kdj_window:      int, 指标计算天数 (默认 60)
      auto_sync:       bool, 若缓存过期, 自动同步日线后再选股 (默认 false, 仅提示)
    """
    strategy = request.args.get("strategy", "kdj")
    top_n = int(request.args.get("top_n", 10))
    buckets_str = request.args.get("buckets", "")
    exclude_st = request.args.get("exclude_st", "true").lower() == "true"
    min_amount_wan = float(request.args.get("min_amount_wan", 500))
    min_list_days = int(request.args.get("min_list_days", 60))
    kdj_window = int(request.args.get("kdj_window", 60))
    auto_sync = request.args.get("auto_sync", "false").lower() == "true"

    # --- 策略级参数 ---
    strategy_cfg = {}
    up_days_raw = request.args.get("up_days")
    if up_days_raw is not None:
        strategy_cfg["up_days"] = int(up_days_raw)
    strict_up_raw = request.args.get("strict_up")
    if strict_up_raw is not None:
        strategy_cfg["strict_up"] = strict_up_raw.lower() in ("true", "1", "yes")

    if buckets_str:
        allowed = [b.strip() for b in buckets_str.split(",") if b.strip()]
    else:
        allowed = default_allowed_buckets()

    freshness = None
    try:
        freshness = _screener.check_cache_freshness()
    except Exception:
        pass

    if freshness and freshness.get("status") == "stale" and auto_sync:
        _log.warning("[screener.pick] 缓存过期 (gap=%s), 自动 sync_kline", freshness.get("gap_days"))
        try:
            _screener.sync_kline(count=max(kdj_window + 20, 80), force=False)
            freshness = _screener.check_cache_freshness()
        except Exception as e:
            _log.error("auto_sync fail: %s", e)

    try:
        result = _screener.pick(
            top_n=top_n,
            allowed_buckets=allowed,
            exclude_st=exclude_st,
            min_amount_wan=min_amount_wan,
            min_list_days=min_list_days,
            kdj_window=kdj_window,
            strategy=strategy,
            strategy_cfg=strategy_cfg if strategy_cfg else None,
        )
        if not result.get("ok"):
            return jsonify(_clean_nan(result)), 400
        if freshness:
            result["cache_freshness"] = freshness
        return jsonify(_clean_nan(result))
    except Exception as e:
        _log.error("screener pick fail: %s", e, exc_info=True)
        return jsonify({"ok": False, "error": str(e)}), 500


@app.get("/api/sync/status")
def api_sync_status():
    """全局同步状态: FundamentalService 是否正在写 fundamental.duckdb"""
    sync = _fundamental.get_sync_status()
    return jsonify({"ok": True, "sync": sync})


@app.get("/api/screener/status")
def api_screener_status():
    """缓存状态概览 — TTL=30s 缓存"""
    cached = _route_cache.get("scr_status")
    if cached is not None:
        return jsonify(cached)
    mv_status = _screener.get_mv_cache_status()
    kl_status = _screener.get_kline_cache_status()
    out = {
        "ok": True,
        "cache": {
            "market_value": mv_status,
            "kline": kl_status,
        },
        "sync": _fundamental.get_sync_status(),
        "allowed_buckets_default": default_allowed_buckets(),
        "biz_choices": ["all", "mv", "kline"],
    }
    _route_cache.set("scr_status", out)
    return jsonify(out)


@app.delete("/api/screener/cache")
def api_screener_cache_delete():
    """删除缓存 (异步, 返回 task_id)

    query params:
      biz:    "mv" | "kline" | "all" (默认 all)
      codes:  逗号分隔的股票代码, 不填=全清
    """
    biz = request.args.get("biz", "all")
    codes_str = request.args.get("codes", "")
    codes = [c.strip() for c in codes_str.split(",") if c.strip()] if codes_str else None

    if biz == "mv":
        tid = _tasks.submit(_screener.delete_mv_cache, codes=codes)
    elif biz == "kline":
        tid = _tasks.submit(_screener.delete_kline_cache, codes=codes)
    elif biz == "all":
        def _do_all(codes=None, progress=None):
            r1 = _screener.delete_mv_cache(codes, progress=progress)
            if progress:
                progress("delete_mv", 1, 1, {"msg": "市值已删, 开始删日线"})
            r2 = _screener.delete_kline_cache(codes, progress=progress)
            return {"mv": r1, "kline": r2}
        tid = _tasks.submit(_do_all, codes=codes)
    else:
        return jsonify({"ok": False, "error": f"unknown biz: {biz}"}), 400

    return jsonify({"ok": True, "task_id": tid, "biz": biz})


# ======================== 用户认证 ========================
@app.post("/api/auth/register")
def api_auth_register():
    data = request.get_json(force=True, silent=True) or {}
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    display_name = (data.get("display_name") or username).strip()
    if not username or not password:
        return jsonify({"error": "用户名和密码必填"}), 400
    try:
        user = _portfolio.register(username, password, display_name)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    session["uid"] = user["id"]
    session.permanent = True
    return jsonify({"ok": True, "user": user})


@app.post("/api/auth/login")
def api_auth_login():
    data = request.get_json(force=True, silent=True) or {}
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    if not username or not password:
        return jsonify({"error": "用户名和密码必填"}), 400
    try:
        user = _portfolio.login(username, password)
    except ValueError as e:
        return jsonify({"error": str(e)}), 401
    session["uid"] = user["id"]
    session.permanent = True
    return jsonify({"ok": True, "user": user})


@app.post("/api/auth/logout")
def api_auth_logout():
    session.pop("uid", None)
    return jsonify({"ok": True})


@app.get("/api/auth/me")
def api_auth_me():
    user = _current_user()
    if user:
        return jsonify({"user": user, "logged_in": True})
    return jsonify({"user": None, "logged_in": False})


@app.get("/api/auth/users")
def api_auth_list_users():
    """后台查看所有用户 (仅已登录可用)"""
    user = _current_user()
    if not user:
        return jsonify({"error": "未登录"}), 401
    return jsonify({"users": _portfolio.list_users()})


# ======================== 搜索辅助 ========================
import threading as _threading
_search_cache = {}
_search_cache_lock = _threading.Lock()
SEARCH_CACHE_TTL = 30

_A_SUFFIX = (".SH", ".SZ", ".BJ")
_SECTOR_PREFIX = ("880", "881", "882", "883", "884", "885", "886", "887", "888", "889")
_IDX_PATTERNS = ("399", "000001.SH")
_FUND_PREFIX = ("15", "16", "18", "50", "51", "52", "53", "54", "55", "56", "58", "59")
_BOND_PREFIX = ("11", "12", "13", "14", "15", "20", "22", "23", "24")


def _classify_code(code, name=""):
    if not code:
        return "other"
    upper = code.upper()
    if upper.startswith(_SECTOR_PREFIX):
        return "sector"
    if upper.startswith("399") or upper == "000001.SH":
        return "index"
    if upper.endswith((".HK", ".US", ".NQ", ".OT")):
        return "other"
    if upper.endswith(".OF"):
        return "fund"
    short = upper.split(".")[0]
    if short.startswith(_FUND_PREFIX) or "ETF" in name.upper() or "LOF" in name.upper():
        return "etf"
    if short.startswith(_BOND_PREFIX) and "债" in name:
        return "bond"
    if upper.endswith(_A_SUFFIX):
        return "stock"
    return "other"


def _is_a_stock(code):
    upper = code.upper()
    return upper.endswith(_A_SUFFIX) and not (
        upper.startswith("399") or upper == "000001.SH" or
        upper.startswith(_SECTOR_PREFIX)
    )


def _score_hit(kw, code, name, hit_index):
    kw_lower = kw.lower()
    code_lower = code.lower()
    name_lower = name.lower()
    score = 100 - hit_index * 0.5
    if code_lower == kw_lower:
        score += 500
    elif code_lower.startswith(kw_lower.split(".")[0]):
        score += 80
    if name_lower.startswith(kw_lower):
        score += 200
    elif kw_lower in name_lower:
        score += 50
    return score


def _normalize_search_results(hits, kw):
    type_priority = {"stock": 0, "index": 1, "sector": 2, "etf": 3, "bond": 4, "fund": 5, "other": 99}
    results = []
    for idx, h in enumerate(hits):
        code = h.get("Code", "")
        name = h.get("Name", "")
        if not code:
            continue
        ctype = _classify_code(code, name)
        if ctype == "other":
            continue
        score = _score_hit(kw, code, name, idx)
        results.append({
            "code": code,
            "name": name,
            "type": ctype,
            "_tp": type_priority.get(ctype, 99),
            "_sc": score,
        })
    results.sort(key=lambda x: (x["_tp"], -x["_sc"], x["code"]))
    out = []
    for r in results:
        out.append({"code": r["code"], "name": r["name"], "type": r["type"]})
    return out


@app.get("/api/search")
def api_search():
    kw = request.args.get("q", "").strip()
    limit = min(int(request.args.get("limit", "15")), 30)
    include_all = request.args.get("all", "0") == "1"

    if not kw or len(kw) < 1:
        return jsonify([])

    import time as _t
    now = _t.time()

    with _search_cache_lock:
        cached = _search_cache.get(kw)
        if cached and now - cached["ts"] < SEARCH_CACHE_TTL:
            return jsonify(cached["hits"][:limit])

    hits = []
    if _tq is not None:
        try:
            raw = _tq.get_match_stkinfo(key_word=kw) or []
            hits = [{"Code": h.get("Code", ""), "Name": h.get("Name", "")} for h in raw]
        except Exception as _e:
            app.logger.warning(f"搜索调用 TQ 失败: {_e}")

    if not include_all:
        results = _normalize_search_results(hits, kw)
    else:
        results = []
        for h in hits:
            code = h.get("Code", "")
            name = h.get("Name", "")
            results.append({
                "code": code, "name": name,
                "type": _classify_code(code, name),
            })

    with _search_cache_lock:
        if len(_search_cache) > 200:
            oldest_keys = sorted(_search_cache.keys(), key=lambda k: _search_cache[k]["ts"])[:100]
            for k in oldest_keys:
                _search_cache.pop(k, None)
        _search_cache[kw] = {"hits": results, "ts": now}

    return jsonify(results[:limit])


@app.get("/api/kline")
def api_kline():
    code = request.args.get("code")
    if not code:
        return jsonify({"error": "缺 code"}), 400
    period = request.args.get("period", "1d")
    n = int(request.args.get("n", "180"))
    dividend = request.args.get("div", "front")
    force = request.args.get("refresh", "0") == "1"
    start = request.args.get("start")
    end = request.args.get("end")

    try:
        result = _kline_svc.get_kline(code, period=period, n=n, dividend=dividend,
                                       force=force, start=start, end=end)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    if result.get("_empty"):
        result.pop("_empty", None)
        return jsonify(result), 404
    return jsonify(result)


@app.get("/api/predict")
def api_predict():
    _user, _err = _require_user()
    if _err: return _err
    code = request.args.get("code")
    if not code:
        return jsonify({"error": "缺 code"}), 400
    try:
        result = _predict.predict(code)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return jsonify(result)


@app.get("/api/predict-sell")
def api_predict_sell():
    """交易预测 —— 根据目标收益率反推售价 (不加仓 / 加仓两种模式)"""
    _user, _err = _require_user()
    if _err: return _err
    _uid = _user["id"]

    code = request.args.get("code")
    if not code:
        return jsonify({"error": "缺 code"}), 400
    target_r = float(request.args.get("target_r", "0")) / 100
    add_qty = int(request.args.get("add_qty", "0"))
    add_price = request.args.get("add_price")
    if add_price is not None:
        add_price = float(add_price)
    manual_n1 = request.args.get("n1")
    manual_a = request.args.get("a")
    if manual_n1 is not None:
        manual_n1 = int(manual_n1)
    if manual_a is not None:
        manual_a = float(manual_a)

    try:
        result = _predict.predict_sell(
            user_id=_uid, code=code, target_r=target_r,
            add_qty=add_qty, add_price=add_price,
            manual_n1=manual_n1, manual_a=manual_a)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    if "error" in result and result.get("mode") is None:
        return jsonify(result), 400
    return jsonify(result)


@app.get("/api/portfolio")

def api_portfolio():
    _user, _err = _require_user()
    if _err: return _err
    _uid = _user["id"]
    """当前持仓: 数量/成本价/现价/收益率 + 账户总资产"""
    pos_df = _portfolio.positions(_uid)
    cash = _portfolio.cash_balance(_uid)
    items = []
    total_mkt = 0.0
    total_cost = 0.0
    for _, r in pos_df.iterrows():
        code = r["code"]
        qty = int(r["quantity"])
        cost = float(r["cost_price"])
        try:
            snap = _tq.get_market_snapshot(stock_code=code) or {}
            now = float(snap.get("Now") or snap.get("LastClose") or cost)
            info = _tq.get_stock_info(stock_code=code) or {}
            name = info.get("Name") or r.get("name") or code
        except Exception:
            now = cost
            name = r.get("name") or code
        mkt = now * qty
        pnl = mkt - cost * qty
        pnl_pct = (pnl / (cost * qty) * 100) if cost > 0 else 0
        total_mkt += mkt
        total_cost += cost * qty
        items.append({
            "code": code, "name": name, "qty": qty,
            "cost": round(cost, 3), "now": round(now, 3),
            "mkt": round(mkt, 2), "pnl": round(pnl, 2),
            "pnl_pct": round(pnl_pct, 2),
        })
    total_pnl = total_mkt - total_cost
    total_pnl_pct = (total_pnl / total_cost * 100) if total_cost > 0 else 0
    return jsonify({
        "cash": round(cash, 2),
        "total_mkt": round(total_mkt, 2),
        "total_cost": round(total_cost, 2),
        "total_asset": round(cash + total_mkt, 2),
        "total_pnl": round(total_pnl, 2),
        "total_pnl_pct": round(total_pnl_pct, 2),
        "positions": items,
    })


@app.post("/api/portfolio/buy")

def api_buy():
    _user, _err = _require_user()
    if _err: return _err
    _uid = _user["id"]
    """买入: body JSON {code, name, price, quantity} 或 {code, cash}"""
    data = request.get_json(force=True, silent=True) or {}
    code = data.get("code")
    if not code:
        return jsonify({"error": "缺少 code"}), 400
    price = float(data.get("price", 0))
    name = data.get("name") or code
    quantity = data.get("quantity")
    cash = data.get("cash")
    try:
        if not price:
            snap = _tq.get_market_snapshot(stock_code=code) or {}
            price = float(snap.get("Now") or snap.get("LastClose") or 0)
        if not name or name == code:
            info = _tq.get_stock_info(stock_code=code) or {}
            name = info.get("Name") or code
    except Exception:
        pass
    if cash:
        quantity = int(float(cash) / price / 100) * 100 or 100
    elif quantity:
        quantity = int(quantity)
    else:
        return jsonify({"error": "需提供 quantity 或 cash"}), 400
    reason = data.get("reason", "web")
    _portfolio.buy(_uid, code, name, price, quantity, reason)
    return jsonify({"ok": True, "code": code, "price": price, "quantity": quantity,
                    "amount": round(price * quantity, 2)})


@app.post("/api/portfolio/sell")

def api_sell():
    _user, _err = _require_user()
    if _err: return _err
    _uid = _user["id"]
    """卖出: body JSON {code, quantity, price?}"""
    data = request.get_json(force=True, silent=True) or {}
    code = data.get("code")
    quantity = int(data.get("quantity", 0))
    if not code or not quantity:
        return jsonify({"error": "缺少 code/quantity"}), 400
    price = data.get("price")
    name = data.get("name")
    try:
        if price is None:
            snap = _tq.get_market_snapshot(stock_code=code) or {}
            price = float(snap.get("Now") or snap.get("LastClose") or 0)
        else:
            price = float(price)
        if not name or name == code:
            info = _tq.get_stock_info(stock_code=code) or {}
            name = info.get("Name") or code
    except Exception:
        if price is None:
            price = 0
        if not name:
            name = code
    reason = data.get("reason", "web")
    # 校验持仓是否足够
    pos_df = _portfolio.positions(_uid)
    row = pos_df[pos_df["code"] == code]
    if row.empty or int(row.iloc[0]["quantity"]) < quantity:
        have = int(row.iloc[0]["quantity"]) if not row.empty else 0
        return jsonify({"error": f"持仓不足: 持有 {have}, 要卖 {quantity}"}), 400
    _portfolio.sell(_uid, code, name, price, quantity, reason)
    return jsonify({"ok": True, "code": code, "name": name, "price": price, "quantity": quantity,
                    "amount": round(price * quantity, 2)})


@app.post("/api/portfolio/cash")

def api_cash():
    _user, _err = _require_user()
    if _err: return _err
    _uid = _user["id"]
    """调整现金/总资产: body {delta} 增减 或 {cash} 直接设置"""
    data = request.get_json(force=True, silent=True) or {}
    reason = data.get("reason", "手动调整")
    try:
        if "cash" in data:
            new_cash = float(data["cash"])
            before = _portfolio.cash_balance(_uid)
            _portfolio.set_cash(_uid, new_cash, reason)
            after = _portfolio.cash_balance(_uid)
            delta = after - before
        elif "delta" in data:
            delta = float(data["delta"])
            _portfolio.adjust_cash(_uid, delta, reason)
            before = _portfolio.cash_balance(_uid) - delta
            after = _portfolio.cash_balance(_uid)
        else:
            return jsonify({"error": "需提供 cash 或 delta"}), 400
        return jsonify({
            "ok": True, "before": round(before, 2), "after": round(after, 2),
            "delta": round(delta, 2), "reason": reason,
            "message": ("增加 " if delta >= 0 else "减少 ") + f"{abs(delta):,.2f} 元" if delta else "无变化",
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.get("/api/watchlist")

def api_watchlist_get():
    _user, _err = _require_user()
    if _err: return _err
    _uid = _user["id"]
    wl = _portfolio.watchlist(_uid)
    items = []
    for _, r in wl.iterrows():
        code = r["code"]
        try:
            snap = _tq.get_market_snapshot(stock_code=code) or {}
            now = float(snap.get("Now") or snap.get("LastClose") or 0)
            preclose = float(snap.get("LastClose") or now or 0)
            pct = round((now / preclose - 1) * 100, 2) if preclose > 0 else 0
            info = _tq.get_stock_info(stock_code=code) or {}
            name = info.get("Name") or r.get("name") or code
        except Exception:
            now = 0; pct = 0; name = r.get("name") or code
        items.append({"code": code, "name": name, "now": round(now, 3), "pct": pct})
    return jsonify(items)


@app.post("/api/watchlist")

def api_watchlist_add():
    _user, _err = _require_user()
    if _err: return _err
    _uid = _user["id"]
    data = request.get_json(force=True, silent=True) or {}
    code = data.get("code")
    if not code:
        return jsonify({"error": "缺少 code"}), 400
    name = data.get("name") or code
    try:
        if not name or name == code:
            info = _tq.get_stock_info(stock_code=code) or {}
            name = info.get("Name") or code
    except Exception:
        pass
    _portfolio.watchlist_add(_uid, code, name)
    return jsonify({"ok": True, "code": code, "name": name})


@app.delete("/api/watchlist")

def api_watchlist_del():
    _user, _err = _require_user()
    if _err: return _err
    _uid = _user["id"]
    code = request.args.get("code")
    if not code:
        return jsonify({"error": "缺少 code"}), 400
    _portfolio.watchlist_remove(_uid, code)
    return jsonify({"ok": True, "code": code})


@app.get("/api/portfolio/trades")

def api_trades():
    _user, _err = _require_user()
    if _err: return _err
    _uid = _user["id"]
    limit = int(request.args.get("limit", "30"))
    offset = int(request.args.get("offset", "0"))
    df, total = _portfolio.trades(_uid, limit=limit, offset=offset)
    if df.empty:
        return jsonify({"trades": [], "total": total})
    rows = []
    for _, r in df.iterrows():
        t = r["trade_time"]
        if hasattr(t, "strftime"):
            t = t.strftime("%Y-%m-%d %H:%M:%S")
        rows.append({
            "time": t,
            "code": r["code"], "name": r["name"],
            "side": r["side"],
            "price": round(float(r["price"]), 3),
            "qty": int(r["quantity"]),
            "quantity": int(r["quantity"]),
            "amount": round(float(r["amount"]), 2),
            "reason": r.get("reason") or "",
            "balance_after": round(float(r["balance_after"]), 2),
            "created_at": t,
        })
    return jsonify({"trades": rows, "total": total})


@app.get("/api/quote")
def api_quote():
    code = request.args.get("code")
    if not code:
        return jsonify({"error": "缺 code"}), 400
    try:
        return jsonify(_quote.quote(code))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.get("/api/sector")
def api_sector():
    """行业/概念/地区板块行情排行"""
    sector_type = request.args.get("type", "industry")
    period = request.args.get("period", "1d")
    try:
        return jsonify(_quote.sector(sector_type=sector_type, period=period))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ======================== 页面 ========================

@app.get("/")
def index():
    return render_template("index.html")


@app.get("/fundamental")
def fundamental_page():
    """基本面数据展示与溯源页面"""
    return render_template("fundamental.html")


@app.get("/screener")
def screener_page():
    """KDJ 选股中心"""
    return render_template("screener.html")



# ================== Admin ==================
@app.get("/admin")
def admin_page():
    if _current_user() is None:
        return render_template("admin.html", require_auth=True), 401
    return render_template("admin.html", require_auth=False)


def _collect_endpoints():
    eps = []
    for r in app.url_map.iter_rules():
        if r.rule.startswith("/api/") and r.endpoint != "static":
            methods = [m for m in r.methods if m not in ("HEAD", "OPTIONS")]
            eps.append({"path": r.rule, "methods": sorted(methods),
                        "enabled": _api_enabled.get(r.rule, True)})
    eps.sort(key=lambda x: x["path"])
    return eps


@app.get("/api/admin/status")
def admin_status():
    global _admin_status_cache
    import sys as _sys, platform as _plat, datetime as _dt, os as _os2, time as _time
    _now = _time.time()
    if _admin_status_cache["data"] and (_now - _admin_status_cache["ts"]) < _ADMIN_STATUS_TTL:
        resp = dict(_admin_status_cache["data"])
        resp["uptime"] = str(_dt.datetime.now() - _START_TIME)
        resp["cache_hit"] = True
        resp["cached_age_s"] = round(_now - _admin_status_cache["ts"], 1)
        return jsonify(resp)

    _t0 = _time.time()
    import flask, duckdb, pandas, pyarrow
    deps = {
        "python": _sys.version.split()[0],
        "flask": getattr(flask, "__version__", "?"),
        "duckdb": getattr(duckdb, "__version__", "?"),
        "pandas": getattr(pandas, "__version__", "?"),
        "pyarrow": getattr(pyarrow, "__version__", "?"),
    }
    tq_ok = _tq is not None
    try:
        snap = _tq.get_market_snapshot(stock_code="002415.SZ") if tq_ok else {}
        tq_ok = bool(snap)
    except Exception:
        tq_ok = False
    cache_scan = {"root": _os2.getcwd(), "nodes": [], "total_kb": 0.0}
    data_dir = str(_cfg.DATA_DIR)
    if _os2.path.isdir(data_dir):
        for _root, _dirs, _files in _os2.walk(data_dir):
            _dirs[:] = [d for d in _dirs if not d.startswith("_")]
            if not _files: continue
            _tb = sum(_os2.path.getsize(_os2.path.join(_root, f)) for f in _files)
            _rel = _os2.path.relpath(_root, _os2.getcwd())
            _rows = 0
            if _rel == "data":
                try:
                    import duckdb as _ddb2
                    for _f in _files:
                        if _f.endswith(".duckdb"):
                            try:
                                _cn = _ddb2.connect(_os2.path.join(_root, _f), read_only=True)
                                for _tbl in _cn.execute("SELECT table_name FROM information_schema.tables WHERE table_schema='main'").fetchall():
                                    _rows += _cn.execute(f'SELECT COUNT(*) FROM "{_tbl[0]}"').fetchone()[0]
                                _cn.close()
                            except Exception: pass
                except Exception: pass
            elif "kline" in _rel:
                try:
                    import pyarrow.parquet as _pq2
                    for _f in _files:
                        if _f.endswith(".parquet"):
                            _rows += _pq2.ParquetFile(_os2.path.join(_root, _f)).metadata.num_rows
                except Exception: pass
            _mt = max((_os2.path.getmtime(_os2.path.join(_root, f)) for f in _files), default=0)
            _ts = _dt.datetime.fromtimestamp(_mt).strftime("%Y-%m-%d %H:%M") if _mt else "-"
            _full = _os2.path.join(_os2.getcwd(), _rel)
            cache_scan["nodes"].append({"name": _rel, "full_path": _full, "size_kb": round(_tb/1024,1), "files": len(_files), "rows": _rows, "mtime": _ts})
        cache_scan["total_kb"] = round(sum(n["size_kb"] for n in cache_scan["nodes"]), 1)

    _elapsed = round((_time.time() - _t0) * 1000, 1)
    resp = {
        "version": "1.3.0",
        "uptime": str(_dt.datetime.now() - _START_TIME),
        "platform": f"{_plat.system()} {_plat.release()}",
        "cwd": os.getcwd(),
        "frozen": bool(getattr(_sys, "frozen", False)),
        "tdx_connected": tq_ok,
        "endpoints": _collect_endpoints(),
        "deps": deps,
        "cache": cache_scan,
        "cache_hit": False,
        "elapsed_ms": _elapsed,
    }
    _admin_status_cache = {"ts": _time.time(), "data": resp, "elapsed_ms": _elapsed}
    return jsonify(resp)


@app.post("/api/admin/toggle")
def admin_toggle():
    from flask import request as _r
    data = _r.get_json(silent=True) or {}
    path = data.get("path", "")
    enable = data.get("enable", True)
    if not path.startswith("/api/"):
        return jsonify({"error": "invalid_path"}), 400
    _api_enabled[path] = bool(enable)
    return jsonify({"path": path, "enabled": _api_enabled[path]})


@app.post("/api/admin/tq-test")
def admin_tq_test():
    """调试接口: 生产环境已安全禁用 (任意反射调用风险)"""
    return jsonify({
        "ok": False,
        "error": "此调试接口已在生产环境禁用 (安全风险: 任意反射调用 TQ 函数)",
        "hint": "如需测试 TQ 接口, 请在开发环境运行 python -c 直接调用 tq_client",
    }), 503


@app.post("/api/admin/shutdown")
def admin_shutdown():
    """优雅关闭: 先 checkpoint 关闭连接, 再退出 (避免 .wal 残留)

    若后台同步线程正占用 DuckDB 连接导致 CHECKPOINT 阻塞,
    看门狗在超时后强制退出 (OS 释放文件句柄, 残留 WAL 下次启动自动重放, 不丢数据)。
    """
    import os as _osx
    import threading
    try:
        def _do_shutdown():
            try:
                _kline_store.con.execute("CHECKPOINT")
                _kline_store.con.close()
                _portfolio.con.execute("CHECKPOINT")
                _portfolio.con.close()
                _fundamental.store.con.execute("CHECKPOINT")
                _fundamental.store.con.close()
            except Exception:
                pass
            _osx._exit(0)
        def _watchdog():
            import time; time.sleep(8)
            _osx._exit(0)
        threading.Thread(target=_do_shutdown, daemon=True).start()
        threading.Thread(target=_watchdog, daemon=True).start()
    except Exception:
        pass
    return jsonify({"ok": True, "message": "服务器正在关闭..."})


@app.post("/api/admin/restart")
def admin_restart():
    return jsonify({
        "ok": True,
        "message": "请关闭当前窗口后重新运行 start.bat / StockQuant.exe",
    })


@app.post("/api/admin/clear-memory")
def admin_clear_memory():
    try:
        _kline_store.con.execute("PRAGMA flush")
        _kline_store.con.execute("CHECKPOINT")
    except Exception:
        pass
    return jsonify({"ok": True})


import os as _os2

@app.post("/api/admin/clear-all-cache")
def admin_clear_all():
    """清理缓存（Parquet 日线 + DuckDB 业务表数据）

    安全策略:
      - Parquet: 直接物理删除（纯缓存）
      - DuckDB:  通过 Flask 进程内部已持有的连接清空业务表数据，保留表结构
                 不再使用独立新连接 dropalltables+os.remove（Windows 下会导致
                 旧连接持有孤儿文件句柄，checkpoint 写入无效，下次启动读到空文件）
      - fundamental/portfolio 的元表(field_meta / update_log / portfolio 表) 不动
    """
    import os as _osx
    stats = {"deleted_parquet": 0, "truncated_duckdb": 0, "errors": []}
    data_dir = str(_cfg.DATA_DIR)
    if not _osx.path.isdir(data_dir):
        return jsonify(stats)

    # ---- 1. 删除 Parquet 日线缓存 ----
    for root, dirs, files in _osx.walk(data_dir):
        dirs[:] = [d for d in dirs if not d.startswith("_")]
        for f in files:
            fp = _osx.path.join(root, f)
            try:
                if f.endswith(".parquet"):
                    _osx.remove(fp)
                    stats["deleted_parquet"] += 1
            except Exception as e:
                stats["errors"].append(f"{fp}: {e}")

    # ---- 2. 清空 fundamental.duckdb 业务数据（保留表结构和元表）----
    try:
        store = _fundamental.store
        business_tables = [
            "stock_info", "stock_more",
            "financial_facts", "gpjy_facts",
            "chip_facts", "l2_facts",
            "shareholder_facts", "mainbusi_facts", "mainbusi_profile",
        ]
        for t in business_tables:
            try:
                store.con.execute(f'DELETE FROM "{t}"')
            except Exception:
                pass
        store.con.execute("CHECKPOINT")
        stats["truncated_duckdb"] += 1
    except Exception as e:
        stats["errors"].append(f"fundamental.duckdb: {e}")

    # ---- 3. 清空 kline.duckdb（日线行情缓存）----
    try:
        kl_con = _kline_store.con
        kl_meta = _kline_store.meta()
        if not kl_meta.empty:
            for t in kl_meta["code"].unique():
                try:
                    kl_con.execute(f'DROP TABLE IF EXISTS "{t}"')
                except Exception:
                    pass
        try:
            kl_con.execute("DELETE FROM kline_meta")
        except Exception:
            pass
        try:
            kl_con.execute("CHECKPOINT")
        except Exception:
            pass
        stats["truncated_duckdb"] += 1
    except Exception as e:
        stats["errors"].append(f"kline.duckdb: {e}")

    return jsonify(stats)


@app.get("/api/admin/db-detail")
def admin_db_detail():
    code = request.args.get("code", "").strip()
    period = request.args.get("period", "1d")
    if code:
        if not _kline_store.has_data(code, period=period):
            return jsonify({"error": "no_data", "code": code, "period": period}), 404
        rng = _kline_store.get_date_range(code, period=period)
        df = _kline_store.load(code, period=period)
        recent = df.tail(5).reset_index()
        recent["date"] = recent["date"].astype(str)
        return jsonify({
            "code": code, "period": period,
            "range": {"min": str(rng["min"]), "max": str(rng["max"]), "rows": rng["rows"]},
            "recent": recent.round(2).to_dict(orient="records"),
        })
    meta = _kline_store.meta()
    if not meta.empty:
        meta["last_date"] = meta["last_date"].astype(str)
        meta["updated_at"] = meta["updated_at"].astype(str)
    return jsonify({"codes": meta.to_dict(orient="records")})


@app.post("/api/admin/db-delete")
def admin_db_delete():
    import json as _json
    raw = request.get_data(as_text=True)
    data = {}
    if raw:
        try:
            data = _json.loads(raw)
        except Exception:
            data = {}
    if not data:
        data = request.args.to_dict()
    code = str(data.get("code", "")).strip()
    period = str(data.get("period", "1d")).strip() or "1d"
    all_flag = str(data.get("all", "")).lower() in ("1", "true", "yes")
    if all_flag:
        _kline_store.delete_all()
        return jsonify({"deleted": "all"})
    if not code:
        return jsonify({"error": "code_required"}), 400
    before = _kline_store.has_data(code, period=period)
    _kline_store.delete_code(code, period=period)
    return jsonify({"code": code, "period": period, "existed": before})


@app.get("/api/admin/flow")
def admin_flow():
    nodes = [
        # ===== LAYER 1: external =====
        {"id":"tdx","name":"通达信客户端","type":"external","domain":"external",
         "info":{'desc': '通达信客户端行情数据源。TQ-Python 所有接口的底层行情引擎。', 'params': [], 'example': '启动通达信客户端并登录账号后，TQ-Python 自动接管行情数据推送。'}},

        # ===== LAYER 2: tq =====
        {"id":"tq_md","name":"get_market_data","type":"tq","sig":"K线 OHLCV","domain":"trade",
         "info":{'desc': '获取 K 线历史数据 (OHLCV)。选股引擎批量同步日线的核心接口。', 'params': [('stock_list', 'list', '股票代码列表'), ('period', 'str', '1d/1w/1mon'), ('count', 'int', 'K线条数')], 'example': "_tq.get_market_data(stock_list=codes, period='1d', count=500)", 'testable': True, 'tq_func': 'get_market_data', 'tq_params': ['stock_code','period']}},
        {"id":"tq_snap","name":"get_market_snapshot","type":"tq","sig":"实时快照","domain":"trade",
         "info":{'desc': '获取实时行情快照 (最新价/涨跌幅/成交量/五档)。', 'params': [('stock_code', 'str', '股票代码')], 'example': "_tq.get_market_snapshot(stock_code='000001.SZ')", 'testable': True, 'tq_func': 'get_market_snapshot', 'tq_params': ['stock_code']}},
        {"id":"tq_info","name":"get_stock_info","type":"tq","sig":"股票详情","domain":"trade",
         "info":{'desc': '获取股票基础信息 (名称/行业/上市日期/总股本/流通股)。', 'params': [('stock_code', 'str', '股票代码')], 'example': "_tq.get_stock_info(stock_code='600519.SH')['name']", 'testable': True, 'tq_func': 'get_stock_info', 'tq_params': ['stock_code']}},
        {"id":"tq_match","name":"get_match_stkinfo","type":"tq","sig":"名称模糊匹配","domain":"trade",
         "info":{'desc': '按名称或代码模糊匹配股票 (搜索联想)。', 'params': [('key_word', 'str', '关键词'), ('limit', 'int', '返回上限')], 'example': "_tq.get_match_stkinfo(key_word='茅台', limit=5)", 'testable': True, 'tq_func': 'get_match_stkinfo', 'tq_params': ['key_word']}},
        {"id":"tq_sector","name":"get_sector_stocks","type":"tq","sig":"板块成分","domain":"trade",
         "info":{'desc': '获取板块 (行业/概念/指数) 成分股列表。', 'params': [('sector', 'str', '板块名')], 'example': "_tq.get_sector_stocks('白酒')", 'testable': True, 'tq_func': 'get_sector_stocks', 'tq_params': ['sector']}},
        {"id":"tq_more","name":"get_more_info","type":"tq","sig":"流通市值/基本面","domain":"screener",
         "info":{'desc': '批量获取股票扩展信息，选股引擎用于拉取流通市值做分桶。', 'params': [('stock_list', 'list', '股票代码列表')], 'example': "_tq.get_more_info(stock_list=all_codes)", 'testable': True, 'tq_func': 'get_more_info', 'tq_params': ['stock_code']}},
        {"id":"tq_sector_list","name":"get_sector_list","type":"tq","sig":"板块列表","domain":"trade",
         "info":{'desc': '获取所有板块 (行业/概念) 清单。', 'params': [], 'example': "_tq.get_sector_list()", 'testable': True, 'tq_func': 'get_sector_list', 'tq_params': []}},
        {"id":"tq_price","name":"price_df","type":"tq","sig":"矩阵→DF","domain":"trade",
         "info":{'desc': '内部工具函数，将 TQ 原始 price 矩阵转为 pandas DataFrame。', 'params': [], 'example': 'K 线入库前标准化步骤。'}},
        {"id":"tq_download","name":"download_file","type":"tq","sig":"落盘文件(down_type=1/5)","domain":"fundamental",
         "info":{'desc': '触发客户端下载文件: down_type=1 十大股东 / down_type=5 主营构成(按产品/按行业/按地区, 带金额)。文件落盘到客户端 PYPlugins/data 目录。', 'params': [('stock_code','str','股票代码'),('down_time','str','年份如 20251231'),('down_type','int','1=股东 5=主营构成')], 'example': "download_file(stock_code='600519.SH', down_time='20251231', down_type=5)", 'testable': True, 'tq_func': 'download_file', 'tq_params': ['stock_code','down_time','down_type']}},

        # ===== LAYER 3: storage =====
        {"id":"parquet","name":"Parquet(KlineStore)","type":"storage","sig":"data/kline/<p>/<code>.parquet","domain":"trade",
         "info":{'desc': 'Parquet 文件缓存。K 线按 period/code 两级目录存储。选股引擎的日线也写这里。', 'params': [], 'example': 'data/kline/1d/000001.SZ.parquet'}},
        {"id":"duck_meta","name":"market.duckdb","type":"storage","sig":"kline_meta + stock_info","domain":"trade",
         "info":{'desc': 'market.duckdb。kline_meta 表 + stock_info 表，用 SQL 替代遍历 Parquet 做 K 线搜索。', 'params': [], 'example': "SELECT COUNT(*) FROM kline_meta WHERE code='000001.SZ'"}},
        {"id":"duck_port","name":"portfolio.duckdb","type":"storage","sig":"持仓+现金+流水+自选","domain":"trade",
         "info":{'desc': 'portfolio.duckdb。持仓表 + 资金账户 + 交易流水 + 自选列表 + 用户认证。', 'params': [], 'example': "SELECT * FROM positions WHERE cash_code='000001.SZ'"}},
        {"id":"duck_fund","name":"fundamental.duckdb","type":"storage","sig":"财务+股东+筹码","domain":"fundamental",
         "info":{'desc': 'fundamental.duckdb。基本面中心独立 DuckDB，存储长表化财务报表、股东明细、筹码分布、主营构成、市场估值。', 'params': [], 'example': "SELECT * FROM financial_facts WHERE code='600519.SZ' LIMIT 50"}},

        # ===== LAYER 4: engine (业务引擎) =====
        {"id":"eng_screener_sync","name":"screener_sync","type":"engine","sig":"批量拉K线+写Parquet","domain":"screener",
         "info":{'desc': '选股引擎·同步。调 get_market_data 批量拉日线 → 写 Parquet；调 get_more_info 拉市值 → 写 DuckDB。', 'params': [], 'example': 'POST /api/screener/sync → _screener.sync_all()'}},
        {"id":"eng_screener_pick","name":"screener_pick","type":"engine","sig":"市值分桶+KDJ筛选","domain":"screener",
         "info":{'desc': '选股引擎·执行。从 Parquet 读日线、market.duckdb 读市值 → KDJ 计算 → 按市值分桶过滤 → 返回 TOP-N。', 'params': [], 'example': 'GET /api/screener/pick → _screener.pick()'}},
        {"id":"eng_screener_kdj","name":"kdj_calculate","type":"engine","sig":"纯数学·无IO","domain":"screener",
         "info":{'desc': 'KDJ 计算模块。RSV=(C-LLV)/(HHV-LLV)*100，K=SMA(RSV,3,1)，D=SMA(K,3,1)，J=3K-2D。纯 pandas 运算，无存储。', 'params': [], 'example': 'J>J_prev>J_prev2 → 向上3天判定'}},
        {"id":"eng_fund_sync","name":"fundamental_sync","type":"engine","sig":"拉财务+写fundamental.duckdb","domain":"fundamental",
         "info":{'desc': '基本面同步引擎。调 TQ 基本面接口 → 写入 fundamental.duckdb。独立于行情域。', 'params': [], 'example': 'POST /api/fundamental/sync'}},

        # ===== LAYER 5: api =====
        # --- 行情交易域 ---
        {"id":"api_kline","name":"GET /api/kline","type":"api","domain":"trade",
         "info":{'desc': '获取 K 线数据。前端蜡烛图直接使用。', 'method': 'GET', 'url': '/api/kline', 'params': [('code','str','股票代码'),('period','str','1d/1w/1mon/1m'),('adjust','str','qfq/hfq/none')], 'example': 'GET /api/kline?code=000001.SZ&period=1d&adjust=qfq', 'testable': True}},
        {"id":"api_search","name":"GET /api/search","type":"api","domain":"trade",
         "info":{'desc': '股票搜索 名称/代码模糊匹配 联想下拉。', 'method': 'GET', 'url': '/api/search', 'params': [('q','str','关键词'),('limit','int','条数上限')], 'example': 'GET /api/search?q=茅台', 'testable': True}},
        {"id":"api_predict","name":"GET /api/predict","type":"api","domain":"trade",
         "info":{'desc': '买入信号预测 AI模型 返回买入概率和目标价。', 'method': 'GET', 'url': '/api/predict', 'params': [('code','str','股票代码'),('period','str','周期')], 'example': 'GET /api/predict?code=000001.SZ', 'testable': True}},
        {"id":"api_pdsl","name":"GET /api/predict-sell","type":"api","domain":"trade",
         "info":{'desc': '卖出信号预测 结合实时快照和持仓评估卖点。', 'method': 'GET', 'url': '/api/predict-sell', 'params': [('code','str','股票代码')], 'example': 'GET /api/predict-sell?code=000001.SZ', 'testable': True}},
        {"id":"api_port","name":"GET /api/portfolio","type":"api","domain":"trade",
         "info":{'desc': '查询持仓账户 总资产/现金/持仓明细/今日盈亏。', 'method': 'GET', 'url': '/api/portfolio', 'params': [], 'example': 'GET /api/portfolio', 'testable': True}},
        {"id":"api_buy","name":"POST /api/portfolio/buy","type":"api","domain":"trade",
         "info":{'desc': '模拟买入 按市价提交买单 更新持仓和流水。', 'method': 'POST', 'url': '/api/portfolio/buy', 'params': [('code','str','股票代码'),('price','float','委托价 0=市价'),('qty','int','股数')], 'example': 'POST {"code":"000001.SZ","price":0,"qty":100}', 'testable': True, 'dangerous': True}},
        {"id":"api_sell","name":"POST /api/portfolio/sell","type":"api","domain":"trade",
         "info":{'desc': '模拟卖出。', 'method': 'POST', 'url': '/api/portfolio/sell', 'params': [('code','str','股票代码'),('price','float','委托价 0=市价'),('qty','int','股数')], 'example': 'POST {"code":"000001.SZ","price":0,"qty":100}', 'testable': True, 'dangerous': True}},
        {"id":"api_watch","name":"WATCHLIST CRUD","type":"api","domain":"trade",
         "info":{'desc': '自选列表增删改查。', 'method': 'CRUD', 'url': '/api/watchlist', 'params': [('GET','-','拉取列表'),('POST','code','添加'),('DELETE','code','删除')], 'example': 'POST /api/watchlist {"code":"000001.SZ"}', 'testable': True}},
        {"id":"api_quote","name":"GET /api/quote","type":"api","domain":"trade",
         "info":{'desc': '实时行情 最新价/涨跌幅/成交量/五档。', 'method': 'GET', 'url': '/api/quote', 'params': [('code','str','股票代码')], 'example': 'GET /api/quote?code=000001.SZ', 'testable': True}},
        {"id":"api_sector","name":"GET /api/sector","type":"api","domain":"trade",
         "info":{'desc': '板块行情 成分股及涨跌。', 'method': 'GET', 'url': '/api/sector', 'params': [('sector','str','板块名')], 'example': 'GET /api/sector?sector=白酒', 'testable': True}},

        # --- 选股域 ---
        {"id":"api_scr_sync","name":"POST /api/screener/sync","type":"api","domain":"screener",
         "info":{'desc': '同步选股缓存 (批量拉日线 + 拉流通市值)。', 'method': 'POST', 'url': '/api/screener/sync', 'params': [('scope','str','all / incremental'),('force','bool','强制重拉')], 'example': 'POST /api/screener/sync', 'testable': True}},
        {"id":"api_scr_pick","name":"POST /api/screener/pick","type":"api","domain":"screener",
         "info":{'desc': '执行 KDJ 选股 返回 TOP-N。', 'method': 'POST', 'url': '/api/screener/pick', 'params': [('buckets','list','市值分桶 C2-C6'),('top_n','int','返回数量'),('min_amount','float','成交额下限')], 'example': 'POST {"buckets":["C3","C4"],"top_n":10}', 'testable': True}},
        {"id":"api_scr_status","name":"GET /api/screener/status","type":"api","domain":"screener",
         "info":{'desc': '选股缓存状态 (市值分桶股票数 / 日线缓存数量)。', 'method': 'GET', 'url': '/api/screener/status', 'params': [], 'example': 'GET /api/screener/status', 'testable': True}},
        {"id":"api_scr_buckets","name":"GET /api/screener/buckets","type":"api","domain":"screener",
         "info":{'desc': '市值分桶定义 + 各桶股票数。', 'method': 'GET', 'url': '/api/screener/buckets', 'params': [], 'example': 'GET /api/screener/buckets', 'testable': True}},
        {"id":"api_scr_cache_del","name":"DELETE /api/screener/cache","type":"api","domain":"screener",
         "info":{'desc': '删除选股缓存 (按股票代码 / 全量 / 按业务类型)。', 'method': 'DELETE', 'url': '/api/screener/cache', 'params': [('codes','list','股票代码列表'),('biz','str','all / kline / market_value')], 'example': 'DELETE /api/screener/cache?biz=all&codes=000001.SZ', 'testable': True, 'dangerous': True}},

        # --- 基本面域 ---
        {"id":"api_fund_sync","name":"POST /api/fundamental/sync","type":"api","domain":"fundamental",
         "info":{'desc': '同步基本面数据 (财务+股东+筹码) → fundamental.duckdb。', 'method': 'POST', 'url': '/api/fundamental/sync', 'params': [('code','str','单只代码 可选')], 'example': 'POST /api/fundamental/sync', 'testable': True}},
        {"id":"api_fund_profile","name":"GET /api/fundamental/profile","type":"api","domain":"fundamental",
         "info":{'desc': '股票基本面档案 (行业/地区/市值/股东)。', 'method': 'GET', 'url': '/api/fundamental/profile', 'params': [('code','str','股票代码')], 'example': 'GET /api/fundamental/profile?code=600519.SH', 'testable': True}},
        {"id":"api_fund_fin","name":"GET /api/fundamental/financial","type":"api","domain":"fundamental",
         "info":{'desc': '财务数据查询 (format=wide 宽表 / format=long 长表)，支持按报告期过滤。', 'method': 'GET', 'url': '/api/fundamental/financial', 'params': [('code','str','股票代码'),('format','str','wide/long'),('report_date','str','报告期 可选 如 20260331'),('fields','str','字段列表 宽表可选')], 'example': 'GET /api/fundamental/financial?code=000002.SZ&format=long&report_date=20260331', 'testable': True}},
        {"id":"api_fund_sum","name":"GET /api/fundamental/summary","type":"api","domain":"fundamental",
         "info":{'desc': '财务摘要 (ROE/PE/PB/营收增速)。', 'method': 'GET', 'url': '/api/fundamental/summary', 'params': [('code','str','股票代码')], 'example': 'GET /api/fundamental/summary?code=600519.SH', 'testable': True}},
        {"id":"api_fund_fields","name":"GET /api/fundamental/fields","type":"api","domain":"fundamental",
         "info":{'desc': '基本面字段字典 (所有可用指标)。', 'method': 'GET', 'url': '/api/fundamental/fields', 'params': [], 'example': 'GET /api/fundamental/fields', 'testable': True}},
        {"id":"api_fund_gpjy","name":"GET /api/fundamental/gpjy","type":"api","domain":"fundamental",
         "info":{'desc': '股东交易 (增减持记录)。', 'method': 'GET', 'url': '/api/fundamental/gpjy', 'params': [('code','str','股票代码')], 'example': 'GET /api/fundamental/gpjy?code=600519.SH', 'testable': True}},
        {"id":"api_fund_chip","name":"GET /api/fundamental/chip","type":"api","domain":"fundamental",
         "info":{'desc': '筹码分布 (获利盘比例/峰位)。', 'method': 'GET', 'url': '/api/fundamental/chip', 'params': [('code','str','股票代码')], 'example': 'GET /api/fundamental/chip?code=600519.SH', 'testable': True}},
        {"id":"api_fund_mainbusi","name":"GET /api/fundamental/mainbusi","type":"api","domain":"fundamental",
         "info":{'desc': '主营构成明细 (按产品/按行业/按地区, 含收入/成本/毛利) + 概述。', 'method': 'GET', 'url': '/api/fundamental/mainbusi', 'params': [('code','str','股票代码'),('report_date','str','报告期 可选')], 'example': 'GET /api/fundamental/mainbusi?code=600519.SH&report_date=20251231', 'testable': True}},

        # --- 系统/管理域 ---
        {"id":"api_admin_status","name":"GET /api/admin/status","type":"api","domain":"admin",
         "info":{'desc': '后台系统状态 (版本/缓存/依赖/连通性)。', 'method': 'GET', 'url': '/api/admin/status', 'params': [], 'example': 'GET /api/admin/status', 'testable': True}},
        {"id":"api_admin_flow","name":"GET /api/admin/flow","type":"api","domain":"admin",
         "info":{'desc': '数据流向图数据 (nodes + edges)。', 'method': 'GET', 'url': '/api/admin/flow', 'params': [], 'example': 'GET /api/admin/flow', 'testable': True}},
        {"id":"api_toggle","name":"POST /api/admin/toggle","type":"api","domain":"admin",
         "info":{'desc': '启用/禁用某个 Endpoint (路由开关)。', 'method': 'POST', 'url': '/api/admin/toggle', 'params': [('path','str','路由路径'),('enable','bool','启用/禁用')], 'example': 'POST {"path":"/api/portfolio","enable":false}', 'testable': True}},
        {"id":"api_clear_cache","name":"POST /api/admin/clear-all-cache","type":"api","domain":"admin",
         "info":{'desc': '清空所有 Parquet + 重建 DuckDB (危险)。', 'method': 'POST', 'url': '/api/admin/clear-all-cache', 'params': [], 'example': 'POST /api/admin/clear-all-cache', 'testable': True, 'dangerous': True}},
        {"id":"api_shutdown","name":"POST /api/admin/shutdown","type":"api","domain":"admin",
         "info":{'desc': '远程关闭 Flask 服务。', 'method': 'POST', 'url': '/api/admin/shutdown', 'params': [], 'example': 'POST /api/admin/shutdown', 'testable': True, 'dangerous': True}},
        {"id":"api_tq_test","name":"POST /api/admin/tq-test","type":"api","domain":"admin",
         "info":{'desc': '在后台模态框里测试任意 TQ 接口。', 'method': 'POST', 'url': '/api/admin/tq-test', 'params': [('func','str','TQ 函数名'),('args','dict','参数字典')], 'example': 'POST {"func":"get_stock_info","args":{"stock_code":"000001.SZ"}}', 'testable': True}},

        # ===== LAYER 6: fe (emoji 图标化) =====
        {"id":"fe_kline","name":"📈 K线图","type":"fe","domain":"trade",
         "info":{'desc': 'K 线蜡烛图 配合 MA/MACD。', 'params': [], 'example': '首页主 K 线区域。'}},
        {"id":"fe_pred","name":"🎯 预测","type":"fe","domain":"trade",
         "info":{'desc': '买入/卖出预测面板。', 'params': [], 'example': 'K 线图上方信号卡片。'}},
        {"id":"fe_trade","name":"🛒 交易","type":"fe","domain":"trade",
         "info":{'desc': '买卖撤单入口。', 'params': [], 'example': '输入代码数量 → 点击买入。'}},
        {"id":"fe_port","name":"💰 持仓","type":"fe","domain":"trade",
         "info":{'desc': '总资产/现金/持仓明细。', 'params': [], 'example': '首页右侧资产卡。'}},
        {"id":"fe_watch","name":"⭐ 自选","type":"fe","domain":"trade",
         "info":{'desc': '自选列表侧边栏。', 'params': [], 'example': '左侧边栏自选。'}},
        {"id":"fe_sector","name":"📊 板块","type":"fe","domain":"trade",
         "info":{'desc': '板块涨跌榜。', 'params': [], 'example': '行业大盘页。'}},
        {"id":"fe_scr","name":"🎲 选股中心","type":"fe","domain":"screener",
         "info":{'desc': 'KDJ 选股 + 市值分桶可视化 + 缓存管理。', 'params': [], 'example': '/screener 页面。'}},
        {"id":"fe_fund","name":"🏢 基本面","type":"fe","domain":"fundamental",
         "info":{'desc': '财务/股东/筹码一站式查询。', 'params': [], 'example': '/fundamental 页面。'}},
        {"id":"fe_admin","name":"⚙️ 后台","type":"fe","domain":"admin",
         "info":{'desc': '系统监控 + 缓存管理 + 流向图。', 'params': [], 'example': '/admin 页面。'}},
    ]

    # edges: [from, to, domain, kind]  kind="real"(写入 实线) or "query"(查询 虚线)
    # domain 决定颜色: trade=black, screener=orange, fundamental=purple, admin=gray
    edges = [
        # --- TQ 来自外部 ---
        ["tdx","tq_md","external","real"],
        ["tdx","tq_snap","external","real"],
        ["tdx","tq_info","external","real"],
        ["tdx","tq_match","external","real"],
        ["tdx","tq_sector","external","real"],
        ["tdx","tq_more","external","real"],
        ["tdx","tq_sector_list","external","real"],

        # --- TQ price_df 工具 ---
        ["tq_md","tq_price","trade","real"],

        # --- 存储层写入 (real=实线) ---
        ["tq_price","parquet","trade","real"],
        ["tq_price","duck_meta","trade","real"],
        ["eng_screener_sync","tq_md","screener","real"],        # 选股同步 → 批量拉K线
        ["eng_screener_sync","tq_more","screener","real"],       # 选股同步 → 拉市值
        ["eng_screener_sync","parquet","screener","real"],       # 选股同步 → 写日线
        ["eng_screener_sync","duck_meta","screener","real"],     # 选股同步 → 写市值
        ["eng_fund_sync","tq_more","fundamental","real"],        # 基本面同步 → 拉TQ
        ["eng_fund_sync","tq_download","fundamental","real"],    # 基本面同步 → 下载股东/主营文件
        ["eng_fund_sync","duck_fund","fundamental","real"],      # 基本面同步 → 写fundamental.duckdb

        # --- 存储层查询回读 (query=虚线) ---
        ["parquet","duck_meta","trade","query"],
        ["duck_meta","parquet","trade","query"],
        ["eng_screener_pick","parquet","screener","query"],     # 选股执行 → 读日线
        ["eng_screener_pick","duck_meta","screener","query"],   # 选股执行 → 读市值
        ["eng_screener_pick","eng_screener_kdj","screener","query"],  # 选股执行 → 调用 KDJ 计算

        # --- API → 存储 / TQ ---
        ["api_kline","parquet","trade","query"],
        ["api_kline","duck_meta","trade","query"],
        ["api_search","tq_match","trade","query"],
        ["api_predict","parquet","trade","query"],
        ["api_predict","duck_meta","trade","query"],
        ["api_pdsl","parquet","trade","query"],
        ["api_pdsl","duck_meta","trade","query"],
        ["api_pdsl","duck_port","trade","query"],
        ["api_pdsl","tq_snap","trade","query"],
        ["api_pdsl","tq_info","trade","query"],
        ["api_port","duck_port","trade","query"],
        ["api_port","tq_snap","trade","query"],
        ["api_port","tq_info","trade","query"],
        ["api_buy","duck_port","trade","real"],
        ["api_buy","tq_snap","trade","query"],
        ["api_buy","tq_info","trade","query"],
        ["api_sell","duck_port","trade","real"],
        ["api_sell","tq_snap","trade","query"],
        ["api_sell","tq_info","trade","query"],
        ["api_watch","duck_port","trade","query"],
        ["api_quote","tq_snap","trade","query"],
        ["api_quote","tq_info","trade","query"],
        ["api_sector","tq_sector","trade","query"],
        ["api_sector","tq_sector_list","trade","query"],

        # --- 选股 API → 引擎 ---
        ["api_scr_sync","eng_screener_sync","screener","real"],
        ["api_scr_pick","eng_screener_pick","screener","query"],
        ["api_scr_status","duck_meta","screener","query"],
        ["api_scr_status","parquet","screener","query"],
        ["api_scr_buckets","duck_meta","screener","query"],
        ["api_scr_cache_del","parquet","screener","real"],
        ["api_scr_cache_del","duck_meta","screener","real"],

        # --- 基本面 API → 存储 ---
        ["api_fund_sync","eng_fund_sync","fundamental","real"],
        ["api_fund_profile","duck_fund","fundamental","query"],
        ["api_fund_fin","duck_fund","fundamental","query"],
        ["api_fund_sum","duck_fund","fundamental","query"],
        ["api_fund_fields","duck_fund","fundamental","query"],
        ["api_fund_gpjy","duck_fund","fundamental","query"],
        ["api_fund_chip","duck_fund","fundamental","query"],
        ["api_fund_mainbusi","duck_fund","fundamental","query"],

        # --- 管理 API ---
        ["api_admin_status","duck_meta","admin","query"],
        ["api_admin_status","duck_port","admin","query"],
        ["api_toggle","duck_port","admin","real"],     # 路由开关持久化

        # --- fe → api ---
        ["fe_kline","api_kline","trade","query"],
        ["fe_kline","api_quote","trade","query"],
        ["fe_pred","api_predict","trade","query"],
        ["fe_pred","api_pdsl","trade","query"],
        ["fe_trade","api_buy","trade","real"],
        ["fe_trade","api_sell","trade","real"],
        ["fe_trade","api_quote","trade","query"],
        ["fe_trade","api_search","trade","query"],
        ["fe_port","api_port","trade","query"],
        ["fe_port","api_watch","trade","query"],
        ["fe_watch","api_watch","trade","query"],
        ["fe_watch","api_search","trade","query"],
        ["fe_sector","api_sector","trade","query"],

        ["fe_scr","api_scr_sync","screener","real"],
        ["fe_scr","api_scr_pick","screener","query"],
        ["fe_scr","api_scr_status","screener","query"],
        ["fe_scr","api_scr_buckets","screener","query"],
        ["fe_scr","api_scr_cache_del","screener","real"],

        ["fe_fund","api_fund_sync","fundamental","real"],
        ["fe_fund","api_fund_profile","fundamental","query"],
        ["fe_fund","api_fund_fin","fundamental","query"],
        ["fe_fund","api_fund_sum","fundamental","query"],
        ["fe_fund","api_fund_fields","fundamental","query"],
        ["fe_fund","api_fund_gpjy","fundamental","query"],
        ["fe_fund","api_fund_chip","fundamental","query"],
        ["fe_fund","api_fund_mainbusi","fundamental","query"],

        ["fe_admin","api_admin_status","admin","query"],
        ["fe_admin","api_admin_flow","admin","query"],
        ["fe_admin","api_toggle","admin","real"],
        ["fe_admin","api_clear_cache","admin","real"],
        ["fe_admin","api_shutdown","admin","real"],
        ["fe_admin","api_tq_test","admin","query"],
    ]
    return jsonify({"nodes": nodes, "edges": edges})

@app.get("/health")
def health():
    return jsonify({"ok": True})

def _fix_cwd_for_exe():
    """打包成 exe 后, cwd 可能是 C:\Windows\System32, 需要切到 exe 所在目录"""
    import sys as _sys, os as _os
    if getattr(_sys, "frozen", False):
        exe_dir = _os.path.dirname(_sys.executable)
        _os.chdir(exe_dir)
        _log.info("[打包模式] 工作目录已切换至: %s", exe_dir)


def _auto_open_browser(host="127.0.0.1", port=8765, delay=1.5):
    """延迟启动浏览器, 等待 Flask 就绪"""
    import threading, time, webbrowser
    url = f"http://{host}:{port}"

    def _open():
        time.sleep(delay)
        try:
            webbrowser.open(url)
        except Exception:
            pass

    threading.Thread(target=_open, daemon=True).start()


def _background_data_sync(codes=None, biz_list=None, progress=None):
    """对库内已有股票按指定业务类型增量同步

    codes: 股票代码列表, None 时从 stock_info 表读取
    biz_list: 业务列表, None 时从 _auto_sync_config 读取
    progress: 进度回调 callable(stage, done, total, info)
    返回: {biz_name: result_dict, ...}
    """
    import time as _time

    if codes is None:
        try:
            df = _fundamental.store.con.execute(
                "SELECT DISTINCT code FROM stock_info").df()
            codes = df["code"].tolist()
        except Exception:
            codes = []
    if not codes:
        _log.info("[自动同步] 无已同步股票, 跳过")
        return {}

    if biz_list is None:
        biz_list = _auto_sync_get_enabled_biz(_auto_sync_config)
    if not biz_list:
        _log.info("[自动同步] 无选中业务, 跳过")
        return {}

    _log.info("[自动同步] %d 只股票 × %d 种业务: %s",
              len(codes), len(biz_list), ",".join(biz_list))

    results = {}
    for biz in biz_list:
        if biz == "stock_basic":
            results[biz] = _fundamental.sync_stock_basic(codes, progress=progress)
        elif biz == "financial":
            results[biz] = _fundamental.sync_financial(codes, progress=progress)
        elif biz == "gpjy":
            results[biz] = _fundamental.sync_gpjy(codes, progress=progress)
        elif biz == "chip":
            results[biz] = _fundamental.sync_chip(codes, days=250, progress=progress)
        elif biz == "l2":
            results[biz] = _fundamental.sync_l2(codes, count=60, progress=progress)
        elif biz == "shareholder":
            results[biz] = _fundamental.sync_shareholder(codes, progress=progress)
        elif biz == "mainbusi":
            results[biz] = _fundamental.sync_mainbusi(codes, progress=progress)

    return results


def _auto_sync_run_async(codes=None, biz_list=None):
    """异步执行自动同步 (启动时 / 手动触发)"""
    def _run():
        delay = _auto_sync_config.get("delay_seconds", 3)
        if delay > 0:
            _log.info("[自动同步] 等待 %d 秒...", delay)
            _time.sleep(delay)
        try:
            results = _background_data_sync(codes, biz_list)
            _auto_sync_config["last_run"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            _auto_sync_config["last_result"] = {
                biz: {k: v for k, v in r.items() if k != "per_code"}
                for biz, r in results.items()
            }
            _save_auto_sync_config(_auto_sync_config)
            total_records = sum(
                r.get("total_records", 0) for r in results.values())
            _log.info("[自动同步] 完成: %s, 共 %d 条",
                      ",".join(results.keys()), total_records)
        except Exception as e:
            _log.error("[自动同步] 失败: %s", e, exc_info=True)

    threading.Thread(target=_run, daemon=True, name="auto-sync").start()






if __name__ == "__main__":
    _fix_cwd_for_exe()
    _host = _cfg.HOST
    _port = _cfg.PORT

    # 优雅退出: checkpoint + 关闭所有 DuckDB 连接, 避免强制退出残留 .wal 被锁
    # (Windows 下残留 wal 会导致下次启动后写事务报 "拒绝访问")
    import atexit as _atexit

    def _graceful_close():
        _log.info("优雅退出: checkpoint 并关闭数据库连接...")
        for _name, _store in (("kline", _kline_store), ("portfolio", _portfolio)):
            try:
                _store.con.execute("CHECKPOINT")
                _store.con.close()
                _log.info("  %s.duckdb 已 checkpoint + 关闭", _name)
            except Exception as _e:
                _log.warning("  %s 关闭失败: %s", _name, _e)
        try:
            _fundamental.store.con.execute("CHECKPOINT")
            _fundamental.store.con.close()
            _log.info("  fundamental.duckdb 已 checkpoint + 关闭")
        except Exception:
            pass
        try:
            _tq.close()
        except Exception:
            pass

    _atexit.register(_graceful_close)

    if _cfg.OPEN_BROWSER:
        _auto_open_browser(port=_port)
    if _auto_sync_config.get("on_startup") and _auto_sync_get_enabled_biz(_auto_sync_config):
        _auto_sync_run_async()
    print(f"服务地址: http://127.0.0.1:{_port} (监听 {_host}, 局域网设备可用本机IP访问)", flush=True)
    app.run(host=_host, port=_port, debug=False, threaded=True)