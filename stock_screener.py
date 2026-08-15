# Last modified: 2026-08-12 21:50:40
"""
股票选股引擎
============
算法: J > K 且 J > D 且 K > D 且 J 连续向上3天
支持市值分桶预筛、本地缓存、自定义缓存周期、查询与删除

数据源:
- 日线: Parquet (data/kline/1d/*.parquet) + KlineStore
- 市值: DuckDB (data/fundamental.duckdb -> stock_more.float_mv)
- TQ 客户端: 通达信原生 tqcenter.tq (关键字参数调用)
"""
import sys, time, json, logging, threading
from datetime import datetime, timedelta
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np
import pandas as pd

import config as _cfg
from logger import get_logger
_log = get_logger("stock_screener")


# ===============================================================
# 市值分桶定义 (6档, 方案3 + C7-9合并)
# ===============================================================
MV_BUCKETS = [
    {"id": "C1", "name": "微盘股",     "lo": 0,    "hi": 20,    "exclude": True},
    {"id": "C2", "name": "小盘股",     "lo": 20,   "hi": 50,    "exclude": False},
    {"id": "C3", "name": "中盘股",     "lo": 50,   "hi": 100,   "exclude": False},
    {"id": "C4", "name": "中大盘股",   "lo": 100,  "hi": 200,   "exclude": False},
    {"id": "C5", "name": "大盘股",     "lo": 200,  "hi": 1000,  "exclude": False},
    {"id": "C6", "name": "超大盘蓝筹", "lo": 1000, "hi": 1e12,  "exclude": False},
]

MV_BUCKET_LOOKUP = {}
for _b in MV_BUCKETS:
    MV_BUCKET_LOOKUP[_b["id"]] = _b


def mv_bucket(mv):
    if mv is None or mv <= 0 or np.isnan(mv):
        return None
    for b in MV_BUCKETS:
        if b["lo"] <= mv < b["hi"]:
            return b["id"]
    return None


def bucket_name(bid):
    b = MV_BUCKET_LOOKUP.get(bid)
    return b["name"] if b else "未知"


def default_allowed_buckets():
    return [b["id"] for b in MV_BUCKETS if not b["exclude"]]


# ===============================================================
# KDJ 计算 (通达信标准 9,3,3)
# ===============================================================
def calc_kdj(df, n=9, m1=3, m2=3):
    if df is None or len(df) < n + m2:
        return None
    high = df["High"].astype(float)
    low = df["Low"].astype(float)
    close = df["Close"].astype(float)

    lowest_low = low.rolling(window=n, min_periods=n).min()
    highest_high = high.rolling(window=n, min_periods=n).max()
    denom = highest_high - lowest_low
    rsv = ((close - lowest_low) / denom * 100).where(denom != 0, 50.0)

    k = rsv.ewm(alpha=1.0 / m1, adjust=False).mean()
    d = k.ewm(alpha=1.0 / m2, adjust=False).mean()
    j = 3 * k - 2 * d

    out = pd.DataFrame({
        "K": k.values, "D": d.values, "J": j.values,
    }, index=df.index)
    return out


# ===============================================================
# 策略框架: 基类 + 注册表
# ===============================================================
class BaseStrategy:
    """选股策略基类

    子类只需实现 evaluate() 与 sort_key():
      - evaluate(df, row): 对单只候选做特征计算与买入判定
        满足条件 -> 返回 dict(指标字段 + 条件标志), 并入最终结果行
        不满足   -> 返回 None
      - sort_key(result_row): 结果排序键 tuple, 越小越靠前
    """

    name = "base"
    description = "策略基类"
    params = {}  # {参数名: {"default": 默认值, "desc": 说明}}

    def evaluate(self, df, row=None, cfg=None):
        raise NotImplementedError

    def sort_key(self, result_row):
        return (0,)

    def describe(self):
        return {"name": self.name, "description": self.description,
                "params": self.params}


class KDJStrategy(BaseStrategy):
    """KDJ 多头排列: J>K 且 J>D 且 K>D 且 J 连续向上 N 天"""

    name = "kdj"
    description = "KDJ 多头: J>K 且 J>D 且 K>D 且 J 连续向上N天, 按 J/J_slope/量比 排序"
    params = {
        "window":    {"default": 60, "desc": "K线计算窗口(交易日)"},
        "n":         {"default": 9,  "desc": "KDJ RSV 周期"},
        "m1":        {"default": 3,  "desc": "KDJ K 平滑周期"},
        "m2":        {"default": 3,  "desc": "KDJ D 平滑周期"},
        "up_days":   {"default": 2,  "desc": "J 连续上涨交易日数 (0=关闭此条件)"},
        "strict_up": {"default": True, "desc": "严格单调(>) 或允许持平(>=)"},
    }

    def evaluate(self, df, row=None, cfg=None):
        cfg = cfg or {}
        kdj = calc_kdj(df)

        up_days = cfg.get("up_days", self.params["up_days"]["default"])
        strict  = cfg.get("strict_up", self.params["strict_up"]["default"])

        min_len = max(up_days + 2, 4)
        if kdj is None or len(kdj) < min_len:
            return None

        j_now = kdj["J"].iloc[-1]
        k_now = kdj["K"].iloc[-1]
        d_now = kdj["D"].iloc[-1]

        cond_J_gt_K = bool(j_now > k_now)
        cond_J_gt_D = bool(j_now > d_now)
        cond_K_gt_D = bool(k_now > d_now)

        if up_days <= 0:
            cond_J_up = True
        else:
            tail_js = kdj["J"].iloc[-(up_days + 1):].values
            op = (lambda a, b: a > b) if strict else (lambda a, b: a >= b)
            cond_J_up = all(op(tail_js[i], tail_js[i - 1]) for i in range(1, len(tail_js)))

        if not (cond_J_gt_K and cond_J_gt_D and cond_K_gt_D and cond_J_up):
            return None

        j_start = kdj["J"].iloc[-(up_days + 1)]
        j_slope = (j_now - j_start) / max(up_days, 1)

        vol_ratio = None
        if "Volume" in df.columns:
            vol_ma20 = df["Volume"].tail(20).mean()
            if vol_ma20 and vol_ma20 > 0:
                vol_ratio = float(df["Volume"].iloc[-1]) / vol_ma20

        chg_pct = None
        if "Close" in df.columns and len(df) >= 2:
            c_now = df["Close"].iloc[-1]
            c_prev = df["Close"].iloc[-2]
            if c_prev and c_prev > 0:
                chg_pct = (c_now - c_prev) / c_prev * 100

        # 截取前 20 行 NaN (rolling+ewm 收敛缓冲)
        trim = 20
        kdj_plot = kdj.iloc[trim:].copy()
        df_plot = df.iloc[trim:]
        dates = df_plot.index.tolist() if "date" not in df_plot.columns else df_plot["date"].tolist()
        if hasattr(dates[0], "strftime") if dates else False:
            dates = [d.strftime("%Y-%m-%d") for d in dates]
        kdj_series = {
            "dates": dates,
            "K": [round(float(v), 2) if pd.notna(v) else None for v in kdj_plot["K"].values],
            "D": [round(float(v), 2) if pd.notna(v) else None for v in kdj_plot["D"].values],
            "J": [round(float(v), 2) if pd.notna(v) else None for v in kdj_plot["J"].values],
        }

        return {
            "strategy": self.name,
            "K": round(k_now, 2),
            "D": round(d_now, 2),
            "J": round(j_now, 2),
            "jk_gap": round(j_now - k_now, 2),
            "J_slope": round(j_slope, 2),
            "vol_ratio": round(vol_ratio, 2) if vol_ratio else None,
            "chg_pct": round(chg_pct, 2) if chg_pct is not None else None,
            "up_days": up_days,
            "strict_up": strict,
            "cond_J_gt_K": cond_J_gt_K,
            "cond_J_gt_D": cond_J_gt_D,
            "cond_K_gt_D": cond_K_gt_D,
            "cond_J_up": cond_J_up,
            "kdj_series": kdj_series,
        }

    def sort_key(self, result_row):
        return (-result_row.get("J", 0),
                -result_row.get("J_slope", 0),
                -(result_row.get("vol_ratio") or 0))


_STRATEGIES = {
    "kdj": KDJStrategy(),
}


def get_strategy(name):
    """按名称取策略实例, 未知策略抛 ValueError"""
    s = _STRATEGIES.get(name)
    if s is None:
        raise ValueError("未知策略: %s, 可用: %s" % (name, list(_STRATEGIES)))
    return s


def list_strategies():
    """注册表内全部策略的元数据列表"""
    return [s.describe() for s in _STRATEGIES.values()]


# ===============================================================
# 选股引擎
# ===============================================================
class StockScreener:
    def __init__(self, data_root=None, tq_client=None, fm_service=None, kl_store=None):
        data_root = data_root or str(_cfg.DATA_DIR)
        self.data_root = Path(data_root)
        self.data_root.mkdir(parents=True, exist_ok=True)
        self.kline_root = self.data_root / "kline"
        self.tq = tq_client
        self.fm_service = fm_service   # FundamentalService 实例 (依赖注入, fundamental.duckdb 唯一持有者)
        self.kl_store = kl_store       # KlineStore 实例 (依赖注入, market.duckdb 唯一持有者)


    # =============================================================
    # TQ 原生 API 适配层
    # =============================================================
    def _tq_list_stocks(self):
        """获取全A股代码列表"""
        sl = self.tq.get_stock_list("5", 1)
        return [s["Code"] for s in sl if isinstance(s, dict)]

    def _tq_name_map(self):
        """获取全A股 {Code: Name} 映射 (零额外成本, 复用 get_stock_list)"""
        sl = self.tq.get_stock_list("5", 1)
        return {s["Code"]: s.get("Name", "")
                for s in sl if isinstance(s, dict) and s.get("Code")}

    def _tq_fetch_kline_batch(self, stock_list, count):
        """批量拉日线 (关键字参数, 返回 DataFrame dict)

        复权口径统一为前复权 "front" —— 与 KlineStore.upsert 默认口径一致,
        避免与库内既有数据混入不同复权口径造成指标失真。
        """
        return self.tq.get_market_data(
            stock_list=stock_list, period="1d", count=count,
            dividend_type="front")

    @staticmethod
    def _extract_code_df(raw, code):
        """从原生 tq get_market_data 返回值里提取单票 OHLCV DataFrame

        raw: TQ 原始返回, 两种兼容格式:
              1) 已 unwrap (get_market_data 当前返回): {code: {"Open":[...], "Close":[...], "Date":[...], ...}}
              2) 未 unwrap: {"Value": {code: {...}}, "ErrorId": 0}
        返回: DataFrame(columns=[Open,High,Low,Close,Volume,Amount], index=date)
        """
        if raw is None:
            return None
        # 兼容已 unwrap 与未 unwrap 两种格式
        if "Value" in raw and isinstance(raw.get("Value"), dict):
            value = raw["Value"]
        else:
            value = raw
        code_data = value.get(code)
        if not isinstance(code_data, dict) or "Close" not in code_data:
            return None

        date_list = code_data.get("Date", [])
        n = len(date_list)
        if n == 0:
            return None

        def _to_float_list(key):
            arr = code_data.get(key) or []
            return [float(x) if x not in (None, "", "0") else 0.0 for x in arr[:n]]

        def _parse_date(s):
            try:
                return pd.to_datetime(s, format="%Y%m%d")
            except Exception:
                return pd.NaT

        dates = [_parse_date(d) for d in date_list[:n]]
        df = pd.DataFrame({
            "Open":   _to_float_list("Open"),
            "High":   _to_float_list("High"),
            "Low":    _to_float_list("Low"),
            "Close":  _to_float_list("Close"),
            "Volume": _to_float_list("Volume"),
            "Amount": _to_float_list("Amount"),
        }, index=pd.DatetimeIndex(dates, name="date"))
        df = df.dropna(subset=["Close"]).sort_index()
        return df if len(df) > 0 else None

    # =============================================================
    # 市值缓存: 委托 FundamentalService (fundamental.duckdb 唯一写入口)
    # =============================================================
    def sync_mv(self, codes=None, force=False, progress=None):
        """代理到 FundamentalService.sync_mv —— fundamental.duckdb 唯一持有者"""
        if self.fm_service is None:
            raise RuntimeError("fm_service 未注入, 请从 web_app 调用 _fundamental.sync_mv()")
        return self.fm_service.sync_mv(codes=codes, force=force, progress=progress)

    def get_mv_cache_status(self):
        if self.fm_service is None:
            return {"error": "fm_service 未注入"}
        return self.fm_service.get_mv_cache_status()

    def delete_mv_cache(self, codes=None, progress=None):
        if self.fm_service is None:
            return {"error": "fm_service 未注入"}
        return self.fm_service.delete_mv_cache(codes=codes, progress=progress)

    # =============================================================
    # 日线缓存: 同步 / 查询 / 删除
    # =============================================================
    def _probe_tdx_latest_date(self, probe_code="000001.SZ"):
        """用通达信探测最近一个交易日 (原生 tq 返回 DataFrame(index=日期)).

        返回 "YYYY-MM-DD" 或 None.
        """
        if self.tq is None:
            return None
        try:
            raw = self.tq.get_market_data(
                stock_list=[probe_code], period="1d",
                count=3, dividend_type="front",
            )
            if isinstance(raw, dict) and "Close" in raw:
                close_df = raw["Close"]
                if probe_code in close_df.columns:
                    date_idx = close_df.index.dropna()
                    if len(date_idx) > 0:
                        return pd.to_datetime(date_idx[-1]).strftime("%Y-%m-%d")
        except Exception as e:
            _log.warning("[probe_tdx] %s", e)
        return None

    def sync_kline(self, codes=None, count=60, force=False, batch_size=50, progress=None):
        if self.tq is None:
            raise RuntimeError("TQ client not available")

        if codes is None:
            codes = self._tq_list_stocks()

        total = len(codes)
        kl = self.kl_store
        t0 = time.time()
        saved, skipped, errors = 0, 0, 0

        # --- 日期新鲜度 probe (仅 force=False, 一次调用即可) ---
        probe_date = None
        if not force:
            probe_date = self._probe_tdx_latest_date()
            if probe_date:
                _log.info("[sync_kline] TDX probe latest=%s", probe_date)

        if progress:
            progress("kline", 0, total, {"force": force, "count": count,
                       "probe_date": probe_date, "msg": "开始同步日线"})

        # ---- Phase 1: 预筛哪些批次需要抓取 (本地文件 I/O, 串行很快) ----
        batches_all = [(start, codes[start:start + batch_size])
                       for start in range(0, total, batch_size)]
        fetch_plan = []   # [(start_index, batch_codes_to_fetch)]
        for start, batch in batches_all:
            if force:
                fetch_plan.append((start, batch))
                continue
            need = []
            for c in batch:
                if not kl.has_data(c, "1d"):
                    need.append(c)
                else:
                    rng = kl.get_date_range(c, "1d")
                    if rng is None or rng["rows"] < count * 0.8:
                        need.append(c)
                    elif probe_date and pd.to_datetime(rng["max"]) < pd.to_datetime(probe_date):
                        need.append(c)
            if not need:
                skipped += len(batch)
            else:
                fetch_plan.append((start, need))

        if fetch_plan:
            _log.info("[sync_kline] pre-filter: %d batches need fetch, %d skipped",
                      len(fetch_plan), skipped)

        # ---- Phase 2: 并发抓取 + 批量写 (worker 只做 HTTP, 写文件走 upsert_many) ----
        KLINE_THREADS = 4
        BATCH_WRITE = 300            # 每攒够 300 只批量并行写一次
        done_lock = threading.Lock()
        done_batches = 0

        def _fetch_batch(start, batch):
            try:
                raw = self._tq_fetch_kline_batch(batch, count)
                return start, batch, raw, None
            except Exception as e:
                return start, batch, None, e

        def _flush_write():
            nonlocal saved, errors
            if not write_items:
                return
            w, e = kl.upsert_many(write_items, period="1d", workers=8)
            saved += w
            errors += e
            write_items.clear()

        write_items = []
        if fetch_plan:
            # 并发期间抑制 TQ 逐请求日志 (文件 handler 是 DEBUG, 锁竞争会拖垮并发)
            _tq_logger = logging.getLogger("tdxlambda.tdx_tq_client")
            _tq_old_level = _tq_logger.level
            _tq_logger.setLevel(logging.WARNING)
            try:
                with ThreadPoolExecutor(max_workers=KLINE_THREADS) as pool:
                    futures = {pool.submit(_fetch_batch, s, b): s
                               for s, b in fetch_plan}
                    for fut in as_completed(futures):
                        start, batch, raw, err = fut.result()
                        with done_lock:
                            done_batches += 1

                        if err:
                            errors += len(batch)
                            _log.warning("sync_kline batch fail: %s (batch_len=%d)", err, len(batch))
                        else:
                            for code in batch:
                                df = None
                                try:
                                    df = self._extract_code_df(raw, code)
                                except Exception as e:
                                    errors += 1
                                    _log.warning("sync_kline extract %s fail: %s", code, e)
                                if df is not None and len(df) > 0:
                                    write_items.append((code, df))
                                else:
                                    errors += 1
                            if len(write_items) >= BATCH_WRITE:
                                _flush_write()

                        done = start + len(batch)
                        if done_batches % 4 == 0 or done_batches >= len(fetch_plan):
                            _log.info("sync_kline %d/%d  saved=%d skipped=%d errors=%d  %.1fs",
                                      done, total, saved, skipped, errors, time.time() - t0)
                        if progress:
                            progress("kline", done, total, {
                                "saved": saved, "skipped": skipped, "errors": errors,
                                "elapsed": round(time.time() - t0, 1),
                            })
            finally:
                _tq_logger.setLevel(_tq_old_level)
            _flush_write()           # 尾部剩余不足一批的票

        result = {"total": total, "saved": saved, "skipped": skipped,
                  "errors": errors, "seconds": round(time.time() - t0, 1)}
        _log.info("sync_kline done: %s", result)
        if progress:
            progress("kline", total, total, result)
        return result

    def get_kline_cache_status(self):
        kl = self.kl_store
        row = kl.con.execute("""
            SELECT
                COUNT(*)                                                    as total,
                MIN(row_count)                                              as min_rows,
                MAX(row_count)                                              as max_rows,
                ROUND(AVG(row_count), 1)                                    as avg_rows,
                MEDIAN(row_count)                                           as median_rows,
                SUM(CASE WHEN row_count < 20 THEN 1 ELSE 0 END)            as lt20,
                SUM(CASE WHEN row_count BETWEEN 20 AND 59 THEN 1 ELSE 0 END) as bet20_59,
                SUM(CASE WHEN row_count BETWEEN 60 AND 119 THEN 1 ELSE 0 END) as bet60_119,
                SUM(CASE WHEN row_count >= 120 THEN 1 ELSE 0 END)           as ge120
            FROM kline_meta WHERE period='1d'
        """).fetchone()
        return {
            "root": str(self.kline_root / "1d"),
            "total": int(row[0] or 0),
            "min_rows": int(row[1] or 0),
            "max_rows": int(row[2] or 0),
            "avg_rows": float(row[3] or 0),
            "median_rows": int(row[4] or 0),
            "buckets": {
                "lt20":      int(row[5] or 0),
                "bet20_59":  int(row[6] or 0),
                "bet60_119": int(row[7] or 0),
                "ge120":     int(row[8] or 0),
            },
        }

    def delete_kline_cache(self, codes=None, progress=None):
        kl = self.kl_store
        target = "all" if codes is None else len(codes)
        if progress:
            progress("delete_kline", 0, target, {"msg": "开始删除日线缓存", "target": target})
        if codes is None:
            kl.delete_all()
        else:
            for i, c in enumerate(codes):
                kl.delete_code(c, "1d")
                if progress and (i + 1) % 100 == 0:
                    progress("delete_kline", i + 1, target, {"msg": f"已删除 {i + 1}/{target}"})
        if progress:
            progress("delete_kline", target, target, {"msg": "日线缓存已删除"})
        return {"deleted": target}

    # =============================================================
    # 选股主流程
    # =============================================================
    def pick(self,
             top_n=10,
             allowed_buckets=None,
             exclude_st=True,
             min_amount_wan=500,
             min_list_days=60,
             kdj_window=60,
             use_cache_only=False,
             strategy="kdj",
             strategy_cfg=None):
        if allowed_buckets is None:
            allowed_buckets = default_allowed_buckets()

        try:
            strat = get_strategy(strategy)
        except ValueError as e:
            return {"ok": False, "error": str(e), "elapsed": 0}

        kl = self.kl_store
        t0 = time.time()
        _log.info("pick start: strategy=%s top_n=%d buckets=%s exclude_st=%s min_amount=%d",
                  strategy, top_n, allowed_buckets, exclude_st, min_amount_wan)

        if self.fm_service is None:
            return {"ok": False, "error": "fm_service 未注入, fundamental.duckdb 不可用", "elapsed": 0}

        candidates = self.fm_service.query_mv_candidates(allowed_buckets, exclude_st=exclude_st)
        if candidates is None or candidates.empty:
            _log.warning("pick: 市值桶候选为空, 可能未同步市值")
            return {"ok": False, "error": "市值缓存为空, 请先同步市值", "elapsed": 0}

        candidate_codes = candidates["code"].tolist()
        _log.info("pick step1: 市值桶候选 %d 只", len(candidate_codes))

        results = []
        cache_hit = 0
        cache_miss = 0
        calc_fail = 0

        for idx, row in candidates.iterrows():
            code = row["code"]
            if not kl.has_data(code, "1d"):
                cache_miss += 1
                _log.info("pick  %s has_data=False", code)
                continue
            try:
                tail_n = max(kdj_window, min_list_days)
                df = kl.load_tail(code, tail_n, period="1d")
            except Exception as e:
                cache_miss += 1
                _log.info("pick  %s load fail: %s", code, e)
                continue

            if len(df) < min_list_days:
                calc_fail += 1
                _log.info("pick  %s len(df)=%d < min_list_days=%d", code, len(df), min_list_days)
                continue

            df = df.tail(kdj_window).copy()

            if min_amount_wan > 0 and "Amount" in df.columns:
                amt_5 = df["Amount"].tail(5).mean()
                if pd.isna(amt_5) or amt_5 < min_amount_wan:
                    calc_fail += 1
                    _log.info("pick  %s amt_5=%.0f < min=%.0f (万元)", code, amt_5 or 0, min_amount_wan)
                    continue

            vol_last = df["Volume"].iloc[-1] if "Volume" in df.columns else None
            if vol_last is None or vol_last == 0:
                calc_fail += 1
                _log.info("pick  %s vol_last=0 (停牌)", code)
                continue

            cache_hit += 1

            strat_fields = strat.evaluate(df, row=row, cfg=strategy_cfg or {})
            if strat_fields is None:
                calc_fail += 1
                continue

            bucket_id = mv_bucket(float(row["float_mv"]))
            raw_name = row.get("name")
            raw_mv = row["float_mv"]
            results.append({
                "code": code,
                "name": "" if (raw_name is None or pd.isna(raw_name)) else str(raw_name),
                "bucket": bucket_id,
                "bucket_name": bucket_name(bucket_id) if bucket_id else "",
                "float_mv": round(float(raw_mv), 2) if (raw_mv is not None and not pd.isna(raw_mv)) else None,
                **strat_fields,
            })

        results.sort(key=strat.sort_key)
        top = results[:top_n]

        elapsed = round(time.time() - t0, 2)
        _log.info("pick done: strategy=%s candidates=%d cache_hit=%d cache_miss=%d match=%d top=%d  %.2fs",
                  strategy, len(candidate_codes), cache_hit, cache_miss, len(results), len(top), elapsed)

        # --- 合并最终生效的策略参数 (默认值 + 传入覆盖) ---
        resolved_cfg = {}
        for k, v in (strategy_cfg or {}).items():
            resolved_cfg[k] = v
        for k, meta in strat.params.items():
            if k not in resolved_cfg:
                resolved_cfg[k] = meta["default"]

        return {
            "ok": True,
            "strategy": strat.name,
            "strategy_cfg": resolved_cfg,
            "total_candidates": len(candidate_codes),
            "cache_hit": cache_hit,
            "cache_miss": cache_miss,
            "match_count": len(results),
            "top_n": top_n,
            "results": top,
            "allowed_buckets": allowed_buckets,
            "elapsed_seconds": elapsed,
            "note": ("部分股票本地无日线缓存, 建议先调用 /api/screener/sync?biz=kline "
                     "同步全量日线后再选股" if cache_miss > 0 else None),
        }

    # =============================================================
    # 一键全量同步
    # =============================================================
    def sync_all(self, force=False, kline_count=60, progress=None):
        t0 = time.time()

        def _wrap(stage_name, weight):
            def _cb(stage, done, total, info):
                if progress:
                    progress("all", done, total, {
                        "stage": stage_name, "done": done, "total": total,
                        "weight": weight, **info,
                    })
            return _cb

        if progress:
            progress("all", 0, 100, {"stage": "mv", "msg": "阶段 1/2: 市值同步"})

        mv_result = self.sync_mv(force=force, progress=_wrap("mv", 0.4))

        if progress:
            progress("all", 40, 100, {"stage": "mv_done", "msg": f"市值完成 {mv_result.get('updated',0)}/{mv_result.get('total',0)}"})
            progress("all", 40, 100, {"stage": "kline", "msg": "阶段 2/2: 日线同步"})

        kl_result = self.sync_kline(force=force, count=kline_count, progress=_wrap("kline", 0.6))

        result = {
            "ok": True,
            "mv": mv_result,
            "kline": kl_result,
            "total_seconds": round(time.time() - t0, 1),
        }
        if progress:
            progress("all", 100, 100, result)
        return result

    # =============================================================
    # 缓存新鲜度检查 (A+B 双校验)
    # =============================================================
    def check_cache_freshness(self, tdx_probe_code="000001.SZ"):
        """A+B 双校验: kline_meta 众数 + 通达信日线探测.

        返回 dict: {
            "status": "fresh" | "stale" | "error",
            "storage_latest": "YYYY-MM-DD",
            "tdx_latest":     "YYYY-MM-DD",
            "gap_days":       int,             # tdx - storage
            "sample_count":   int,             # 参与统计的股票数
            "message":        str,
            "ts":             float,            # unix 时间戳
        }
        """
        import datetime as _dt
        ts = time.time()
        result = {
            "status": "fresh",
            "storage_latest": None,
            "tdx_latest": None,
            "gap_days": 0,
            "sample_count": 0,
            "message": "",
            "ts": ts,
        }

        # ---- A: kline_meta 众数 ----
        storage_date = None
        sample_count = 0
        try:
            kl = self.kl_store
            if hasattr(kl, "con"):
                rows = kl.con.execute(
                    "SELECT CAST(last_date AS VARCHAR) AS d, COUNT(*) AS c "
                    "FROM kline_meta WHERE period='1d' "
                    "GROUP BY last_date ORDER BY c DESC LIMIT 1"
                ).fetchone()
                if rows:
                    storage_date = rows[0]
                    total = kl.con.execute(
                        "SELECT COUNT(*) FROM kline_meta WHERE period='1d'"
                    ).fetchone()[0]
                    sample_count = total
        except Exception as e:
            _log.warning(f"[check_cache] kline_meta query failed: {e}")

        if storage_date:
            # 标准化: "2026-08-12" -> date
            storage_date = pd.to_datetime(storage_date).strftime("%Y-%m-%d")
            result["storage_latest"] = storage_date
            result["sample_count"] = sample_count

        # ---- B: 通达信探测 (复用 _probe_tdx_latest_date) ----
        tdx_date = self._probe_tdx_latest_date(tdx_probe_code)
        if tdx_date:
            result["tdx_latest"] = tdx_date

        # ---- 综合判断 ----
        if storage_date and tdx_date:
            gap = (pd.Timestamp(tdx_date) - pd.Timestamp(storage_date)).days
            result["gap_days"] = gap
            if gap <= 0:
                result["status"] = "fresh"
                result["message"] = f"K线数据最新 ({storage_date}), 无需更新"
            elif gap == 1:
                result["status"] = "stale"
                result["message"] = f"K线落后 1 天 (本地 {storage_date} vs 通达信 {tdx_date})"
            else:
                result["status"] = "stale"
                result["message"] = f"K线落后 {gap} 天 (本地 {storage_date} vs 通达信 {tdx_date})"
        elif storage_date and not tdx_date:
            # TDX 探测失败, 退回只用 A
            result["status"] = "fresh"
            result["message"] = f"通达信探测失败, 信任本地: 最新 {storage_date}"
        elif not storage_date and tdx_date:
            result["status"] = "stale"
            result["message"] = f"本地无缓存, 通达信最新 {tdx_date}"
        else:
            result["status"] = "error"
            result["message"] = "无法获取存储状态和通达信数据"

        return result
