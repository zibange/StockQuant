# Last modified: 2026-08-13 01:10:00
"""
基本面数据服务 —— 获取/处理/存储/查询编排层
============================================
职责: 组合 TQLocalClient(数据源) + FundamentalStore(存储),
     对上层暴露统一、高内聚的数据服务 API。

业务功能:
- sync_stock_basic   同步公司基础信息 + 扩展估值信息 (快照)
- sync_financial     同步专业财务数据 (FN1-584, 全历史报告期)
- sync_gpjy          同步股票交易专业数据 (GP 系列)
- sync_chip          同步筹码指标 (MCST/CYS/ASR/SCR/CYC, 每票每日)
- sync_shareholder   同步十大股东/十大流通股东明细 (download_file)
- get_*              统一查询入口 (本地库优先)

扩展约定:
- 新增数据域只需: 数据源加方法 -> 存储层加表 -> 服务层加 sync_/get_
- 外部数据源(如 TDX MCP)后续通过 adapter 接入, 不侵入本服务
"""
import os
import sys
import json
import time
import logging
import threading
import functools
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd

import config as _cfg
from tdx_tq_client import TQLocalClient, get_client, TQError
from fundamental_store import FundamentalStore
from fundamental_fields import FN_NAME, FN_ALL, GP_NAME, GP_ALL
from logger import get_logger
_log = get_logger("fundamental_service")


def _std_out():
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


_std_out()


def _with_sync_lock(biz_label):
    """业务级互斥装饰器: 同一时刻只允许一个 sync_xxx 运行.

    重要说明 (为什么这是业务锁而不是 DB 锁):
      DuckDB MVCC 本身已经支持多连接并发读写互不阻塞. 本锁解决的是
      "业务级并发保护": 不想让 sync_chip (处理 5552 只票, 30min+) 和
      sync_shareholder 同时跑占 CPU + 网络资源.

    策略: blocking=False — 锁被占用时当前调用立即 skipped 返回, 不排队.
    状态: _sync_status['active']=True 时前端可通过 /api/sync/status 轮询到.
    """
    def decorator(func):
        @functools.wraps(func)
        def wrapper(self, *args, **kwargs):
            lock = self._sync_lock
            if not lock.acquire(blocking=False):
                _log.warning("[%s] sync 进行中, 跳过本次调用", biz_label)
                return {"ok": False, "skipped": True,
                        "error": "sync_in_progress",
                        "status": self.get_sync_status()}
            try:
                self._sync_status = {
                    "active": True,
                    "biz": biz_label,
                    "started_at": datetime.now().isoformat(),
                    "thread": threading.current_thread().name,
                }
                _log.info("[%s] sync_lock acquired, 开始同步", biz_label)
                try:
                    return func(self, *args, **kwargs)
                finally:
                    _log.info("[%s] sync 结束, 释放锁", biz_label)
                    self._sync_status = {"active": False, "biz": None}
            finally:
                lock.release()
        return wrapper
    return decorator


class FundamentalService:
    """基本面数据编排服务"""

    def __init__(self, root=None, client=None, store=None):
        root = root or str(_cfg.DATA_DIR)
        self.root = root
        self.client = client or get_client()
        self.store = store or FundamentalStore(root)
        self._sync_lock = threading.RLock()
        self._sync_status = {"active": False, "biz": None}
        # 登记字段元数据 (幂等)
        self.store.register_fields(FN_NAME, "financial", "get_financial_data")
        self.store.register_fields(GP_NAME, "gpjy", "get_gpjy_value")

    # ===============================================================
    # 同步状态查询 (供前端实时显示 + 后端读方法快速检测)
    # ===============================================================
    def get_sync_status(self):
        """返回当前 fundamental.duckdb 写状态 (线程安全快照)"""
        return dict(self._sync_status)

    def is_syncing(self):
        """快速判断: fundamental.duckdb 是否有写操作在进行"""
        return self._sync_status.get("active", False)

    # ===============================================================
    # 同步: 公司基础 + 扩展信息
    # ===============================================================
    @_with_sync_lock("stock_basic")
    def sync_stock_basic(self, codes, force=False, progress=None):
        """同步 get_stock_info + get_more_info 快照

        codes: 股票代码列表; force=False 时仅补缺失票
        progress: callable(stage, done, total, info_dict)
        返回: {ok, updated, skipped, errors}
        """
        if not codes:
            return {"ok": True, "updated": 0, "skipped": 0, "errors": 0}
        _log.info("[sync_stock_basic] start: %d codes, force=%s", len(codes), force)
        total = len(codes)
        t0 = time.time()
        if progress:
            progress("stock_basic", 0, total, {"msg": "开始同步基础信息", "force": force})
        started = datetime.now()
        updated = skipped = errors = 0
        for i, code in enumerate(codes):
            try:
                if not force:
                    exist = self.store.get_stock_info(code)
                    if not exist.empty:
                        skipped += 1
                        continue
                info = self.client.get_stock_info(code) or {}
                more = self.client.get_more_info(code) or {}
                info["code"] = code
                more["code"] = code
                if "Name" not in info:
                    info["Name"] = more.get("Name")
                self.store.upsert_stock_info(info)
                self.store.upsert_stock_more(more)
                updated += 1
            except Exception as e:
                errors += 1
                _log.error("[sync_stock_basic] %s error: %s", code, e)
                self.store.log_update("stock_basic", code, "info+more",
                                      "error", str(e), started, datetime.now())
            if progress and ((i + 1) % 50 == 0 or (i + 1) >= total):
                progress("stock_basic", i + 1, total, {
                    "updated": updated, "skipped": skipped, "errors": errors,
                    "elapsed": round(time.time() - t0, 1),
                })
        self.store.log_update("stock_basic", f"codes={len(codes)}",
                              f"updated={updated} skipped={skipped} errors={errors}",
                              "ok", "", started, datetime.now())
        elapsed = round(time.time() - t0, 1)
        _log.info("[sync_stock_basic] done: updated=%d skipped=%d errors=%d  (%.1fs)",
                  updated, skipped, errors, elapsed)

        # 数据一致性校验: skipped 很多但表实际行数远小于 skipped → 连接/文件路径异常
        if skipped > total * 0.5:
            try:
                actual = self.store.con.execute(
                    "SELECT COUNT(*) FROM stock_info").fetchone()[0]
                if actual < skipped * 0.5:
                    _log.warning(
                        "[sync_stock_basic] 数据一致性告警: "
                        "skipped=%d 但 stock_info 实际仅 %d 行. "
                        "可能是 DuckDB 连接指向了错误文件或刚被清空过",
                        skipped, actual)
            except Exception:
                pass

        if progress:
            progress("stock_basic", total, total, {
                "updated": updated, "skipped": skipped, "errors": errors,
                "elapsed": elapsed, "msg": "基础信息同步完成",
            })
        return {"ok": True, "updated": updated, "skipped": skipped,
                "errors": errors}

    # ===============================================================
    # 同步: 专业财务数据 (FN1-584)
    # ===============================================================
    @_with_sync_lock("financial")
    def sync_financial(self, codes, report_type="report_time",
                       start_time=None, end_time=None, batch_size=10,
                       progress=None):
        """同步专业财务数据, 全量 FN 字段 (批量拉取, 效率高)

        report_type: report_time=按报告期 / announce_time=按公告日期
        batch_size: 每次请求的股票数 (接口按股票分页, 批量显著提速)
        progress: callable(stage, done, total, info_dict)
        """
        if not codes:
            return {"ok": True, "total_records": 0, "errors": 0}
        _log.info("[sync_financial] start: %d codes, report_type=%s", len(codes), report_type)
        total = len(codes)
        t0 = time.time()
        if progress:
            progress("financial", 0, total, {"msg": "开始同步财务数据", "report_type": report_type})
        started = datetime.now()
        total_records = 0
        errors = 0
        batches = (total + batch_size - 1) // batch_size
        for batch_idx, i in enumerate(range(0, len(codes), batch_size)):
            chunk = codes[i:i + batch_size]
            try:
                raw = self.client.get_financial_data(
                    stock_list=chunk, field_list=FN_ALL,
                    start_time=start_time, end_time=end_time,
                    report_type=report_type)
                if isinstance(raw, dict) and "Value" in raw \
                        and isinstance(raw.get("Value"), dict):
                    raw = raw["Value"]
                if not isinstance(raw, dict):
                    continue
                for code, col_data in raw.items():
                    if not isinstance(col_data, dict):
                        continue
                    df = FundamentalService._cols_to_df(col_data, code)
                    n = self.store.upsert_financial(code, df)
                    total_records += n
            except Exception as e:
                errors += 1
                _log.error("[sync_financial] batch %s error: %s", chunk, e)
                self.store.log_update("financial", ",".join(chunk),
                                      report_type, "error", str(e),
                                      started, datetime.now())
            if progress:
                done = min(i + batch_size, total)
                progress("financial", done, total, {
                    "total_records": total_records, "errors": errors,
                    "batch": batch_idx + 1, "total_batches": batches,
                    "elapsed": round(time.time() - t0, 1),
                })
        self.store.log_update("financial", f"codes={len(codes)}",
                              f"records={total_records} errors={errors}",
                              "ok", "", started, datetime.now())
        elapsed = round(time.time() - t0, 1)
        _log.info("[sync_financial] done: %d records, %d errors  (%.1fs)",
                  total_records, errors, elapsed)
        if progress:
            progress("financial", total, total, {
                "total_records": total_records, "errors": errors,
                "elapsed": elapsed, "msg": "财务数据同步完成",
            })
        return {"ok": True, "total_records": total_records, "errors": errors}

    @staticmethod
    def _parse_financial(raw, code):
        """get_financial_data 返回结构 -> 长表 DataFrame

        实际返回: result.Value = {code: {FN1: [v1,v2..], FN2: [...],
                        announce_time: [...], tag_time: [...]}}   (列式)
        兼容: 行式 [{tag_time, announce_time, FN1..}, ...]
        """
        if not raw:
            return pd.DataFrame()
        # 兼容结果包装: raw 可能是 {Value: {code: {...}}} 或 {code: {...}} 或行式
        if isinstance(raw, dict):
            # 解包 result.Value 包装
            if "Value" in raw and isinstance(raw.get("Value"), dict) \
                    and code in raw.get("Value", {}) \
                    and isinstance(raw["Value"][code], dict):
                col_data = raw["Value"][code]
                return FundamentalService._cols_to_df(col_data, code)
            # 情况1: {code: {字段: [..]}} 列式
            if code in raw and isinstance(raw[code], dict):
                col_data = raw[code]
                return FundamentalService._cols_to_df(col_data, code)
            # 情况2: 列式 dict (无 code 嵌套), 如 {FN1: [..], tag_time: [..]}
            n = 0
            for v in raw.values():
                if isinstance(v, (list, tuple)):
                    n = max(n, len(v))
            if n > 0:
                rows = []
                for i in range(n):
                    row = {"code": code}
                    for k, v in raw.items():
                        row[k] = v[i] if isinstance(v, (list, tuple)) and i < len(v) else v
                    rows.append(row)
                if rows:
                    df = pd.DataFrame(rows)
                    return FundamentalService._normalize_financial_cols(df)
            # 情况3: 行式 list of dict
            if raw.get("Value") and isinstance(raw.get("Value"), list):
                return FundamentalService._normalize_financial_cols(
                    pd.DataFrame(raw["Value"]))
            return pd.DataFrame()
        # 情况4: 直接 list (行式)
        if isinstance(raw, list):
            if not raw:
                return pd.DataFrame()
            return FundamentalService._normalize_financial_cols(pd.DataFrame(raw))
        return pd.DataFrame()

    @staticmethod
    def _cols_to_df(col_data, code):
        """列式 {字段: [值..]} -> 行式 DataFrame"""
        n = 0
        for v in col_data.values():
            if isinstance(v, (list, tuple)):
                n = max(n, len(v))
        if n <= 0:
            return pd.DataFrame()
        rows = []
        for i in range(n):
            row = {"code": code}
            for k, v in col_data.items():
                row[k] = v[i] if isinstance(v, (list, tuple)) and i < len(v) else v
            rows.append(row)
        return FundamentalService._normalize_financial_cols(pd.DataFrame(rows))

    @staticmethod
    def _normalize_financial_cols(df):
        """统一报告期/公告日期列名"""
        rename = {}
        if "tag_time" in df.columns and "report_date" not in df.columns:
            rename["tag_time"] = "report_date"
        if "announce_time" in df.columns and "announce_date" not in df.columns:
            rename["announce_time"] = "announce_date"
        if rename:
            df = df.rename(columns=rename)
        return df

    # ===============================================================
    # 同步: 股票交易专业数据 (GP 系列)
    # ===============================================================
    @_with_sync_lock("gpjy")
    def sync_gpjy(self, codes, field_list=None,
                  start_time=None, end_time=None, batch_size=10,
                  progress=None):
        """同步 GP1~GP46 交易专业数据 (批量拉取)

        progress: callable(stage, done, total, info_dict)
        """
        if not codes:
            return {"ok": True, "total_records": 0, "errors": 0}
        _log.info("[sync_gpjy] start: %d codes", len(codes))
        total = len(codes)
        t0 = time.time()
        if progress:
            progress("gpjy", 0, total, {"msg": "开始同步GP交易数据"})
        started = datetime.now()
        if field_list is None:
            field_list = [f"GP{i:02d}" for i in range(1, 47)]
        total_records = 0
        errors = 0
        batches = (total + batch_size - 1) // batch_size
        for batch_idx, i in enumerate(range(0, len(codes), batch_size)):
            chunk = codes[i:i + batch_size]
            try:
                raw = self.client.get_gpjy_value(
                    stock_list=chunk, field_list=field_list,
                    start_time=start_time, end_time=end_time)
                if isinstance(raw, dict) and "Value" in raw \
                        and isinstance(raw.get("Value"), dict):
                    raw = raw["Value"]
                if not isinstance(raw, dict):
                    continue
                for code, stock_data in raw.items():
                    if not isinstance(stock_data, dict):
                        continue
                    df = FundamentalService._gpjy_to_df(stock_data, code)
                    n = self.store.upsert_gpjy(code, df)
                    total_records += n
            except Exception as e:
                errors += 1
                _log.error("[sync_gpjy] batch %s error: %s", chunk, e)
                self.store.log_update("gpjy", ",".join(chunk), "GP1-46",
                                      "error", str(e), started, datetime.now())
            if progress:
                done = min(i + batch_size, total)
                progress("gpjy", done, total, {
                    "total_records": total_records, "errors": errors,
                    "batch": batch_idx + 1, "total_batches": batches,
                    "elapsed": round(time.time() - t0, 1),
                })
        self.store.log_update("gpjy", f"codes={len(codes)}",
                              f"records={total_records} errors={errors}",
                              "ok", "", started, datetime.now())
        elapsed = round(time.time() - t0, 1)
        _log.info("[sync_gpjy] done: %d records, %d errors  (%.1fs)",
                  total_records, errors, elapsed)
        if progress:
            progress("gpjy", total, total, {
                "total_records": total_records, "errors": errors,
                "elapsed": elapsed, "msg": "GP交易数据同步完成",
            })
        return {"ok": True, "total_records": total_records, "errors": errors}

    @staticmethod
    def _gpjy_to_df(stock_data, code):
        """get_gpjy_value 单票数据 {GP01: [{Date, Value:[..]}, ...]} -> DataFrame"""
        if not isinstance(stock_data, dict):
            return pd.DataFrame()
        date_map = {}   # Date -> {field: value_list}
        for field_code, items in stock_data.items():
            if not isinstance(items, list):
                continue
            for item in items:
                if not isinstance(item, dict):
                    continue
                d = str(item.get("Date") or "")
                if not d:
                    continue
                date_map.setdefault(d, {})[field_code] = item.get("Value")
        rows = []
        for d in sorted(date_map.keys()):
            row = {"code": code, "trade_date": d}
            for field_code, vals in date_map[d].items():
                if isinstance(vals, (list, tuple)):
                    if len(vals) == 1:
                        row[field_code] = vals[0]
                    else:
                        for i, v in enumerate(vals, 1):
                            row[f"{field_code}_{i}"] = v
                else:
                    row[field_code] = vals
            rows.append(row)
        return pd.DataFrame(rows)

    @staticmethod
    def _parse_gpjy(raw, code):
        """get_gpjy_value 返回结构 -> 长表 DataFrame

        实际返回: result.Value = {code: {GP01: [{Date, Value:[v1,v2]}, ...], ...}}
        每个字段为 {Date, Value: [若干值]} 序列; 展开为行: trade_date + GP 值
        """
        if not raw:
            return pd.DataFrame()
        # 解包 Value 包装
        if isinstance(raw, dict) and "Value" in raw and isinstance(raw.get("Value"), dict):
            raw = raw["Value"]
        if not isinstance(raw, dict):
            return pd.DataFrame()
        stock_data = raw.get(code)
        if not isinstance(stock_data, dict):
            return pd.DataFrame()

        # 收集所有 Date 并按键值对齐
        date_map = {}   # Date -> {field: value_list}
        for field_code, items in stock_data.items():
            if not isinstance(items, list):
                continue
            for item in items:
                if not isinstance(item, dict):
                    continue
                d = str(item.get("Date") or "")
                if not d:
                    continue
                entry = date_map.setdefault(d, {})
                entry[field_code] = item.get("Value")

        rows = []
        for d in sorted(date_map.keys()):
            row = {"code": code, "trade_date": d}
            entry = date_map[d]
            for field_code, vals in entry.items():
                if isinstance(vals, (list, tuple)):
                    if len(vals) == 1:
                        row[field_code] = vals[0]
                    else:
                        # 多值字段 (如 GP01 两个值), 展开为 field_code_1/2
                        for i, v in enumerate(vals, 1):
                            row[f"{field_code}_{i}"] = v
                else:
                    row[field_code] = vals
            rows.append(row)
        return pd.DataFrame(rows)

    # ===============================================================
    # 同步: 筹码指标 (公式直取, 路线A)
    # ===============================================================
    # 筹码指标公式配置: {公式名: (参数, 说明)}
    CHIP_FORMULAS = {
        "MCST": ("", "市场成本价"),
        "CYS":  ("", "市场盈亏(获利盘%)"),
        "ASR":  ("", "浮筹比例"),
        "SCR":  ("90", "筹码集中度(P1=90)"),
        "CYC":  ("5", "成本均线(CYC1/2/3)"),
    }

    @_with_sync_lock("chip")
    def sync_chip(self, codes, days=250, batch_size=10, progress=None):
        """同步筹码指标: MCST/CYS/ASR/SCR/CYC 每票每日

        通过 formula_process_mul_zb 批量拉取, 按日期入库到 chip_facts
        days: 回看K线天数 (决定可回填历史长度, 250≈1年)
        batch_size: 每次公式调用的股票数
        progress: callable(stage, done, total, info_dict)
        """
        if not codes:
            return {"ok": True, "total_records": 0, "errors": 0}
        _log.info("[sync_chip] start: %d codes, days=%d", len(codes), days)
        total = len(codes)
        t0 = time.time()
        if progress:
            progress("chip", 0, total, {
                "msg": "开始同步筹码指标", "days": days,
                "formulas": list(self.CHIP_FORMULAS.keys()),
            })
        started = datetime.now()
        total_records = 0
        errors = 0
        batches = (total + batch_size - 1) // batch_size
        for batch_idx, i in enumerate(range(0, len(codes), batch_size)):
            chunk = codes[i:i + batch_size]
            try:
                for formula, (arg, _desc) in self.CHIP_FORMULAS.items():
                    raw = self.client.formula_process_mul_zb(
                        formula_name=formula, formula_arg=arg,
                        return_count=days, return_date=True,
                        stock_list=chunk, stock_period="1d",
                        count=days + 30, dividend_type=0)
                    if not isinstance(raw, dict):
                        continue
                    for code, lines in raw.items():
                        if code == "ErrorId" or not isinstance(lines, dict):
                            continue
                        df = self._formula_lines_to_df(lines)
                        if df.empty:
                            continue
                        n = self.store.upsert_metric_facts(
                            "chip_facts", code, df, date_col="trade_date")
                        total_records += n
            except Exception as e:
                errors += 1
                _log.error("[sync_chip] batch %s error: %s", chunk, e)
                self.store.log_update("chip", ",".join(chunk),
                                      "MCST/CYS/ASR/SCR/CYC",
                                      "error", str(e), started, datetime.now())
            if progress:
                done = min(i + batch_size, total)
                elapsed = round(time.time() - t0, 1)
                remaining = (elapsed / (done / total) - elapsed) if done > 0 else 0
                progress("chip", done, total, {
                    "total_records": total_records, "errors": errors,
                    "batch": batch_idx + 1, "total_batches": batches,
                    "elapsed": elapsed,
                    "remaining_est": round(max(0, remaining), 1),
                })
        self.store.log_update("chip", f"codes={len(codes)}",
                              f"records={total_records} errors={errors}",
                              "ok", "", started, datetime.now())
        elapsed = round(time.time() - t0, 1)
        _log.info("[sync_chip] done: %d records, %d errors  (%.1fs)",
                  total_records, errors, elapsed)
        if progress:
            progress("chip", total, total, {
                "total_records": total_records, "errors": errors,
                "elapsed": elapsed, "msg": "筹码指标同步完成",
            })
        return {"ok": True, "total_records": total_records, "errors": errors}

    @staticmethod
    def _formula_lines_to_df(lines):
        """公式返回 {指标: [{'Date':..,'Value':..}, ...]} -> DataFrame

        输出列: trade_date + 各指标列 (多线如 CYC1/CYC2/CYC3 自动展开)
        """
        date_map = {}
        for field_name, items in lines.items():
            if not isinstance(items, list):
                continue
            for item in items:
                if not isinstance(item, dict):
                    continue
                d = str(item.get("Date") or "")
                if not d:
                    continue
                date_map.setdefault(d, {})[field_name] = item.get("Value")
        if not date_map:
            return pd.DataFrame()
        rows = []
        for d in sorted(date_map.keys()):
            row = {"trade_date": d}
            row.update(date_map[d])
            rows.append(row)
        return pd.DataFrame(rows)

    # ===============================================================
    # 同步: L2 扩展日线 (增量积累, 登录时自动触发)
    # ===============================================================
    @_with_sync_lock("l2")
    def sync_l2(self, codes, count=60, batch_size=10, progress=None):
        """同步 L2 扩展日线 (get_exday_data) — 增量积累策略

        ⚠️ 依赖客户端支持 get_exday_data; 当前版本可能返回"不支持该方法"。
        接口不可用时静默跳过并记日志, 不影响其他数据; 客户端升级后自动生效。
        每次登录/触发时拉取近期 count 条并 upsert, 随时间推进数据自然累积。
        progress: callable(stage, done, total, info_dict)
        返回: 每票新增/更新的记录数
        """
        if not codes:
            return {"ok": True, "total_records": 0, "errors": 0, "per_code": {}}
        _log.info("[sync_l2] start: %d codes, count=%d", len(codes), count)
        total = len(codes)
        t0 = time.time()
        if progress:
            progress("l2", 0, total, {"msg": "开始同步L2扩展日线", "count": count})
        started = datetime.now()
        total_records = 0
        per_code = {}
        unsupported = False
        for i, code in enumerate(codes):
            try:
                raw = self.client.get_exday_data(stock_code=code, count=count) or []
                df = self._exday_to_df(raw)
                n = self.store.upsert_metric_facts("l2_facts", code, df,
                                                   date_col="trade_date")
                total_records += n
                per_code[code] = n
            except TQError as e:
                if "MCP" in str(e) or "不支持" in str(e):
                    unsupported = True
                    _log.warning("[sync_l2] 客户端不支持 get_exday_data, 整体跳过")
                    break
                _log.error("[sync_l2] %s TQError: %s", code, e)
                self.store.log_update("l2", code, f"count={count}",
                                      "error", str(e), started, datetime.now())
            except Exception as e:
                _log.error("[sync_l2] %s error: %s", code, e)
                self.store.log_update("l2", code, f"count={count}",
                                      "error", str(e), started, datetime.now())
            if progress and ((i + 1) % 50 == 0 or (i + 1) >= total):
                progress("l2", i + 1, total, {
                    "total_records": total_records,
                    "elapsed": round(time.time() - t0, 1),
                })
        if unsupported:
            self.store.log_update("l2", "ALL", "get_exday_data",
                                  "error", "客户端不支持 get_exday_data (需升级)",
                                  started, datetime.now())
        else:
            self.store.log_update("l2", f"codes={len(codes)}",
                                  f"records={total_records}",
                                  "ok", "", started, datetime.now())
            elapsed = round(time.time() - t0, 1)
            _log.info("[sync_l2] done: %d records  (%.1fs)",
                      total_records, elapsed)
            if progress:
                progress("l2", total, total, {
                    "total_records": total_records,
                    "elapsed": elapsed,
                    "msg": "L2同步完成",
                })
        return {"ok": True, "total_records": total_records, "errors": 0,
                "per_code": per_code, "unsupported": unsupported}

    @staticmethod
    def _exday_to_df(raw):
        """get_exday_data 返回 List[Dict] -> DataFrame (trade_date + 指标列)

        字段: Date, CJBS, Vol[4x4], Amo[4x4], VolNum[2x2], BOrder/BCancel/
        SOrder/SCancel, BuyAvp/SellAvp, TotalBOrder/TotalSOrder
        矩阵字段展开为 field_row_col, 便于长表存储
        """
        if not raw:
            return pd.DataFrame()
        rows = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            d = str(item.get("Date") or "")
            if not d:
                continue
            row = {"trade_date": d}
            for k, v in item.items():
                if k == "Date":
                    continue
                if isinstance(v, (list, tuple)):
                    # 矩阵展开: Vol -> Vol_1_1, Vol_1_2 ...
                    for i, sub in enumerate(v, 1):
                        if isinstance(sub, (list, tuple)):
                            for j, val in enumerate(sub, 1):
                                row[f"{k}_{i}_{j}"] = val
                        else:
                            row[f"{k}_{i}"] = sub
                else:
                    row[k] = v
            rows.append(row)
        return pd.DataFrame(rows)

    # ===============================================================
    # 同步: 十大股东/十大流通股东明细 (download_file down_type=1)
    # ===============================================================
    SHAREHOLDER_YEARS = 5   # 默认回看最近 N 年

    @_with_sync_lock("shareholder")
    def sync_shareholder(self, codes, years=None, data_dir=None,
                         force=False, progress=None):
        """同步十大股东/十大流通股东明细 (download_file down_type=1)

        流程: 逐票逐年调用 download_file 触发客户端下载 -> 读取落盘 JSON
        (PYPlugins/data/holders{code}_{year}.json) -> 解析入库。
        down_time 只生效年份, 每个报告期含 gd(十大股东)+ltgd(十大流通股东)。
        years: 回看年份数, 默认最近 SHAREHOLDER_YEARS 年。
        progress: callable(stage, done, total, info_dict)
        返回: {ok, total_records, errors, per_code}
        """
        if not codes:
            return {"ok": True, "total_records": 0, "errors": 0, "per_code": {}}
        _log.info("[sync_shareholder] start: %d codes, years=%s", len(codes), years or self.SHAREHOLDER_YEARS)
        total = len(codes)
        t0 = time.time()
        if progress:
            progress("shareholder", 0, total, {
                "msg": "开始同步十大股东", "years": years or self.SHAREHOLDER_YEARS,
            })
        started = datetime.now()
        this_year = datetime.now().year
        if years is None:
            years = self.SHAREHOLDER_YEARS
        year_list = [str(this_year - i) for i in range(int(years))]
        total_records = 0
        per_code = {}
        errors = 0
        for i, code in enumerate(codes):
            if not force and self.store.has_shareholder(code):
                per_code[code] = 0
                if progress and ((i + 1) % 50 == 0 or (i + 1) >= total):
                    progress("shareholder", i + 1, total, {
                        "total_records": total_records, "errors": errors,
                        "skipped": per_code,
                        "elapsed": round(time.time() - t0, 1),
                    })
                continue
            n = 0
            try:
                for year in year_list:
                    try:
                        self.client.download_file(
                            stock_code=code, down_time=f"{year}1231",
                            down_type=1)
                    except TQError as e:
                        errors += 1
                        _log.error("[sync_shareholder] %s year=%s download: %s", code, year, e)
                        self.store.log_update(
                            "shareholder", code, f"year={year}",
                            "error", str(e), started, datetime.now())
                        continue
                    raw = self.client.read_holders_file(code, year,
                                                        data_dir=data_dir)
                    if not raw:
                        continue
                    df = self._holders_to_df(raw)
                    if df.empty:
                        continue
                    n += self.store.upsert_shareholder(code, df)
            except Exception as e:
                errors += 1
                _log.error("[sync_shareholder] %s all: %s", code, e)
                self.store.log_update("shareholder", code, "all",
                                      "error", str(e), started,
                                      datetime.now())
            total_records += n
            per_code[code] = n
            if progress and ((i + 1) % 50 == 0 or (i + 1) >= total):
                progress("shareholder", i + 1, total, {
                    "total_records": total_records, "errors": errors,
                    "elapsed": round(time.time() - t0, 1),
                })
        self.store.log_update("shareholder", f"codes={len(codes)}",
                              f"records={total_records} errors={errors}",
                              "ok", "", started, datetime.now())
        elapsed = round(time.time() - t0, 1)
        _log.info("[sync_shareholder] done: %d records, %d errors  (%.1fs)",
                  total_records, errors, elapsed)
        if progress:
            progress("shareholder", total, total, {
                "total_records": total_records, "errors": errors,
                "elapsed": elapsed, "msg": "股东同步完成",
            })
        return {"ok": True, "total_records": total_records, "errors": errors,
                "per_code": per_code}

    @staticmethod
    def _holders_to_df(raw):
        """十大股东落盘 JSON -> DataFrame

        落盘结构: [{gdxx: '{报告期: {gd:[{pm,name,cgsl,cgbl}..],
                              ltgd:[{..}..]}}'}, ...]
        输出列: report_date, holder_type(gd/ltgd), rank, holder_name,
                shares(持股量), pct(持股比例%)
        """
        if not raw:
            return pd.DataFrame()
        rows = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            gdxx = item.get("gdxx")
            if not gdxx:
                continue
            try:
                data = json.loads(gdxx) if isinstance(gdxx, str) else gdxx
            except (ValueError, TypeError):
                continue
            if not isinstance(data, dict):
                continue
            for report_date, body in data.items():
                if not isinstance(body, dict):
                    continue
                for htype in ("gd", "ltgd"):
                    holders = body.get(htype)
                    if not isinstance(holders, list):
                        continue
                    for h in holders:
                        if not isinstance(h, dict):
                            continue
                        try:
                            rank = int(h.get("pm") or 0)
                        except (TypeError, ValueError):
                            continue
                        rows.append({
                            "report_date": str(report_date),
                            "holder_type": htype,
                            "rank": rank,
                            "holder_name": str(h.get("name") or ""),
                            "shares": FundamentalService._to_num(h.get("cgsl")),
                            "pct": FundamentalService._to_num(h.get("cgbl")),
                        })
        if not rows:
            return pd.DataFrame()
        return pd.DataFrame(rows)

    @staticmethod
    def _to_num(v):
        try:
            if v is None or v == "" or v == "-":
                return None
            return float(v)
        except (TypeError, ValueError):
            return None

    # ===============================================================
    # 同步: 主营构成明细 (download_file down_type=5)
    # ===============================================================
    MAINBUSI_YEARS = 3   # 默认回看最近 N 年

    @_with_sync_lock("mainbusi")
    def sync_mainbusi(self, codes, years=None, data_dir=None, force=False,
                      progress=None):
        """同步主营构成明细 (download_file down_type=5)

        流程: 逐票逐年调用 download_file 触发客户端下载 -> 读取落盘 JSON
        (PYPlugins/data/mainbusi{code}_{year}.json) -> 解析入库。
        报告期以文件内 JSON 键为准 (如 20260331), 每文件含
        按产品(项目)/按地区 两个维度 + 概述(产品名称/主营构成文本)。
        years: 回看年份数, 默认最近 MAINBUSI_YEARS 年。
        progress: callable(stage, done, total, info_dict)
        返回: {ok, total_records, errors, per_code}
        """
        if not codes:
            return {"ok": True, "total_records": 0, "errors": 0, "per_code": {}}
        _log.info("[sync_mainbusi] start: %d codes, years=%s", len(codes),
                  years or self.MAINBUSI_YEARS)
        total = len(codes)
        t0 = time.time()
        if progress:
            progress("mainbusi", 0, total, {
                "msg": "开始同步主营构成", "years": years or self.MAINBUSI_YEARS,
            })
        started = datetime.now()
        this_year = datetime.now().year
        if years is None:
            years = self.MAINBUSI_YEARS
        year_list = [str(this_year - i) for i in range(int(years))]
        total_records = 0
        per_code = {}
        errors = 0
        for i, code in enumerate(codes):
            if not force and self.store.has_mainbusi(code):
                per_code[code] = 0
                if progress and ((i + 1) % 50 == 0 or (i + 1) >= total):
                    progress("mainbusi", i + 1, total, {
                        "total_records": total_records, "errors": errors,
                        "elapsed": round(time.time() - t0, 1),
                    })
                continue
            n = 0
            profile_done = False
            try:
                for year in year_list:
                    try:
                        self.client.download_file(
                            stock_code=code, down_time=f"{year}1231",
                            down_type=5)
                    except TQError as e:
                        errors += 1
                        _log.error("[sync_mainbusi] %s year=%s download: %s",
                                   code, year, e)
                        self.store.log_update(
                            "mainbusi", code, f"year={year}",
                            "error", str(e), started, datetime.now())
                        continue
                    raw = self.client.read_mainbusi_file(code, year,
                                                         data_dir=data_dir)
                    if not raw:
                        continue
                    df, profile = self._mainbusi_parse(raw)
                    if df is not None and not df.empty:
                        n += self.store.upsert_mainbusi(code, df)
                    if profile and not profile_done:
                        self.store.upsert_mainbusi_profile(
                            code, profile.get("product_name") or None,
                            profile.get("business_desc") or None,
                            raw_json=json.dumps(profile, ensure_ascii=False))
                        profile_done = True
            except Exception as e:
                errors += 1
                _log.error("[sync_mainbusi] %s all: %s", code, e)
                self.store.log_update("mainbusi", code, "all",
                                      "error", str(e), started,
                                      datetime.now())
            total_records += n
            per_code[code] = n
            if progress and ((i + 1) % 50 == 0 or (i + 1) >= total):
                progress("mainbusi", i + 1, total, {
                    "total_records": total_records, "errors": errors,
                    "elapsed": round(time.time() - t0, 1),
                })
        self.store.log_update("mainbusi", f"codes={len(codes)}",
                              f"records={total_records} errors={errors}",
                              "ok", "", started, datetime.now())
        elapsed = round(time.time() - t0, 1)
        _log.info("[sync_mainbusi] done: %d records, %d errors  (%.1fs)",
                  total_records, errors, elapsed)
        if progress:
            progress("mainbusi", total, total, {
                "total_records": total_records, "errors": errors,
                "elapsed": elapsed, "msg": "主营构成同步完成",
            })
        return {"ok": True, "total_records": total_records, "errors": errors,
                "per_code": per_code}

    @staticmethod
    def _mainbusi_parse(raw):
        """主营构成落盘 JSON -> (明细 DataFrame, 概述 dict)

        落盘结构: [{'zygc': str}, ...], zygc 为 JSON 字符串:
          - 概述: {"产品名称": "...", "主营构成": "..."}  (文本描述)
          - 明细: {"20260331": {"按产品(项目)": [{"主营构成", "主营收入(元)",
                      "收入比例%", "主营成本", "成本比例%", "毛利",
                      "利润比例%", "毛利率%"}], "按行业": [...], "按地区": [...]}}
        注意: 客户端落盘的 zygc 值外层 dict 常缺失右括号 (缺 '}'), 用
        补齐括号方式容错解析。
        输出明细列: report_date, dim_type, item_name, revenue, revenue_pct,
                    cost, cost_pct, profit, profit_pct, profit_rate
        概述字典: {"product_name", "business_desc"}
        """
        import re
        if not raw:
            return None, None

        def _loads_loose(s):
            # 先原样解析, 失败则逐个补齐缺失右括号重试
            for cand in (s, s + "}", s + "}}", s + "]", s + "}]", s + "}]}"):
                try:
                    return json.loads(cand)
                except (ValueError, TypeError):
                    continue
            return None

        rows = []
        profile = None
        for item in raw:
            if not isinstance(item, dict):
                continue
            zygc = item.get("zygc")
            if not zygc:
                continue
            data = _loads_loose(zygc) if isinstance(zygc, str) else zygc
            if not isinstance(data, dict):
                continue
            # 概述部分: 无 8 位数字报告期键 -> 产品名称/主营构成文本
            if "产品名称" in data or (
                    "主营构成" in data and not any(
                        re.fullmatch(r"\d{8}", k) for k in data)):
                profile = {
                    "product_name": str(data.get("产品名称") or ""),
                    "business_desc": str(data.get("主营构成") or ""),
                }
                continue
            for report_date, body in data.items():
                if not isinstance(body, dict):
                    continue
                for dim_key in ("按产品(项目)", "按行业", "按地区"):
                    items = body.get(dim_key)
                    if not isinstance(items, list):
                        continue
                    for it in items:
                        if not isinstance(it, dict):
                            continue
                        name = str(it.get("主营构成") or "")
                        if not name:
                            continue
                        rows.append({
                            "report_date": str(report_date),
                            "dim_type": dim_key,
                            "item_name": name,
                            "revenue": FundamentalService._to_num(
                                it.get("主营收入(元)")),
                            "revenue_pct": FundamentalService._to_num(
                                it.get("收入比例%")),
                            "cost": FundamentalService._to_num(
                                it.get("主营成本")),
                            "cost_pct": FundamentalService._to_num(
                                it.get("成本比例%")),
                            "profit": FundamentalService._to_num(
                                it.get("毛利")),
                            "profit_pct": FundamentalService._to_num(
                                it.get("利润比例%")),
                            "profit_rate": FundamentalService._to_num(
                                it.get("毛利率%")),
                        })
        df = pd.DataFrame(rows) if rows else pd.DataFrame()
        return df, profile

    # ===============================================================
    # 同步: 市值/估值快照 (原 StockScreener.sync_mv 迁入, 统一 fundamental.duckdb 写入口)
    # ===============================================================
    @_with_sync_lock("mv")
    def sync_mv(self, codes=None, force=False, progress=None):
        """同步 stock_more (市值+估值) + stock_info (名称).

        数据源: 复用 get_stock_list 拿 {Code: Name} 做 names upsert, 再逐票调
                get_more_info 拿估值指标 (市值/PE/换手率/量比等).
        归属:  FundamentalService 独有写入口; StockScreener 选股时只读此表.

        codes: None=全市场; 或指定 list[code]
        force: False 时, updated_at < 7 天的票跳过
        progress: callable(stage, done, total, info_dict), 供前端进度条
        返回:   {total, updated, skipped, errors, seconds}
        """
        if codes is None:
            try:
                raw = self.client.get_stock_list("5", 1)
                codes = [s["Code"] for s in raw if isinstance(s, dict) and s.get("Code")]
            except Exception as e:
                _log.error("[sync_mv] get_stock_list failed: %s", e)
                return {"ok": False, "error": str(e), "total": 0}

        total = len(codes)
        t0 = time.time()

        # ---- 批量 upsert 名称 (复用 get_stock_list, 零额外 API 成本) ----
        name_map = {}
        try:
            raw = self.client.get_stock_list("5", 1)
            name_map = {s["Code"]: s.get("Name", "")
                        for s in raw if isinstance(s, dict) and s.get("Code")}
        except Exception as e:
            _log.warning("[sync_mv] name_map fetch failed: %s", e)

        if name_map:
            try:
                rows = [(c, name_map.get(c, "")) for c in codes if name_map.get(c)]
                if rows:
                    # DuckDB executemany 逐行事务极慢(5553行~4分钟), 改用 DataFrame 批量
                    name_df = pd.DataFrame(rows, columns=["code", "name"])
                    self.store.con.register("_name_tmp", name_df)
                    try:
                        self.store.con.execute("""
                            INSERT INTO stock_info(code, name, updated_at)
                            SELECT code, name, CURRENT_TIMESTAMP FROM _name_tmp
                            ON CONFLICT(code) DO UPDATE SET
                                name=excluded.name,
                                updated_at=excluded.updated_at""")
                    finally:
                        self.store.con.unregister("_name_tmp")
                    _log.info("sync_mv: upserted %d names into stock_info", len(rows))
            except Exception as e:
                _log.warning("sync_mv: stock_info name upsert failed: %s", e)

        if progress:
            progress("mv", 0, total, {"force": force, "msg": "开始同步市值"})

        updated = skipped = errors = 0

        # ---- 批量 skip 查询 (一次 SQL 替代 5553 次单票 SELECT) ----
        need_fetch = list(codes)
        if not force:
            try:
                fresh_rows = self.store.con.execute(
                    "SELECT code FROM stock_more WHERE updated_at > NOW() - INTERVAL 7 DAY"
                ).fetchall()
                fresh_set = {r[0] for r in fresh_rows}
                prev_total = len(need_fetch)
                need_fetch = [c for c in need_fetch if c not in fresh_set]
                skipped = prev_total - len(need_fetch)
                _log.info("sync_mv: skip pre-filter %d fresh, %d to fetch", skipped, len(need_fetch))
            except Exception as e:
                _log.warning("sync_mv: bulk skip query failed, fallback to all-fetch: %s", e)

        # ---- 并发抓取 get_more_info (worker 只做 HTTP, 主线程批量写库) ----
        THREADS = 8
        BATCH_WRITE = 200          # 每凑够 200 条批量写一次 DuckDB
        done_counter = 0
        done_lock = threading.Lock()
        pending = []               # 待批量写入的 stock_more dict

        def _fetch_one(code):
            try:
                mi = self.client.get_more_info(code)
                return code, mi, None
            except Exception as e:
                return code, None, e

        def _flush_pending():
            nonlocal updated, errors
            if not pending:
                return
            try:
                self.store.upsert_stock_mores(pending)
                updated += len(pending)
            except Exception as e:
                errors += len(pending)
                _log.warning("sync_mv batch upsert %d rows fail: %s", len(pending), e)
            finally:
                pending.clear()

        if need_fetch:
            # 并发期间抑制 TQ 逐请求日志 (文件 handler 是 DEBUG, 锁竞争会拖垮并发)
            _tq_logger = logging.getLogger("tdxlambda.tdx_tq_client")
            _tq_old_level = _tq_logger.level
            _tq_logger.setLevel(logging.WARNING)
            try:
                with ThreadPoolExecutor(max_workers=THREADS) as pool:
                    futures = {pool.submit(_fetch_one, c): c for c in need_fetch}
                    for fut in as_completed(futures):
                        code, mi, err = fut.result()
                        with done_lock:
                            done_counter += 1

                        if err:
                            errors += 1
                            _log.warning("sync_mv %s fail: %s", code, err)
                        elif mi:
                            mi["code"] = code
                            pending.append(mi)
                            if len(pending) >= BATCH_WRITE:
                                _flush_pending()

                        if done_counter % 200 == 0 or done_counter >= len(need_fetch):
                            _log.info("sync_mv progress %d/%d  updated=%d skipped=%d errors=%d  %.1fs",
                                      done_counter, len(need_fetch), updated, skipped, errors,
                                      time.time() - t0)
                            if progress:
                                progress("mv", done_counter + skipped, total, {
                                    "updated": updated, "skipped": skipped, "errors": errors,
                                    "elapsed": round(time.time() - t0, 1),
                                })
            finally:
                _tq_logger.setLevel(_tq_old_level)
            _flush_pending()       # 尾部剩余不足一批的记录

        result = {"total": total, "updated": updated, "skipped": skipped,
                  "errors": errors, "seconds": round(time.time() - t0, 1)}
        _log.info("sync_mv done: %s", result)
        if progress:
            progress("mv", total, total, result)
        return result

    def get_mv_cache_status(self):
        """市值缓存状态 (总/新鲜/分桶计数)"""
        from stock_screener import MV_BUCKETS
        try:
            total = self.store.con.execute(
                "SELECT COUNT(*) FROM stock_more").fetchone()[0]
            if total == 0:
                return {"total": 0, "fresh": 0, "stale": 0}
            fresh = self.store.con.execute(
                "SELECT COUNT(*) FROM stock_more WHERE updated_at > NOW() - INTERVAL 7 DAY"
            ).fetchone()[0]
            stale = total - fresh
            buckets = {}
            for b in MV_BUCKETS:
                if b["hi"] >= 1e11:
                    r = self.store.con.execute(
                        "SELECT COUNT(*) FROM stock_more WHERE float_mv >= ? AND float_mv > 0",
                        [b["lo"]]).fetchone()[0]
                else:
                    r = self.store.con.execute(
                        "SELECT COUNT(*) FROM stock_more WHERE float_mv >= ? AND float_mv < ?",
                        [b["lo"], b["hi"]]).fetchone()[0]
                buckets[b["id"]] = r
            return {"total": total, "fresh": fresh, "stale": stale, "buckets": buckets}
        except Exception as e:
            return {"error": str(e)}

    def delete_mv_cache(self, codes=None, progress=None):
        """删除市值缓存 (全部 / 指定代码)"""
        target = "all" if codes is None else len(codes)
        if progress:
            progress("delete_mv", 0, target, {"msg": "开始删除市值缓存", "target": target})
        if codes is None:
            self.store.con.execute("DELETE FROM stock_more")
        else:
            self.store.con.executemany(
                "DELETE FROM stock_more WHERE code=?", [(c,) for c in codes])
        if progress:
            progress("delete_mv", target, target, {"msg": "市值缓存已删除"})
        return {"deleted": target}

    def query_mv_candidates(self, allowed_buckets, exclude_st=True):
        """选股引擎候选集查询 (只读, 选股领域唯一跨领域读基本面的入口).

        返回 DataFrame: code, float_mv, name, pe_dyna, hsl, is_st, is_quit
        """
        from stock_screener import MV_BUCKET_LOOKUP
        conds, args = [], []
        for bid in allowed_buckets:
            b = MV_BUCKET_LOOKUP.get(bid)
            if b is None:
                continue
            if b["hi"] >= 1e11:
                conds.append("(sm.float_mv >= ?)")
                args.append(b["lo"])
            else:
                conds.append("(sm.float_mv >= ? AND sm.float_mv < ?)")
                args.extend([b["lo"], b["hi"]])
        if not conds:
            import pandas as pd
            return pd.DataFrame()
        sql = f"""
            SELECT sm.code, sm.float_mv, si.name, sm.pe_dyna, sm.hsl,
                   si.is_st, si.is_quit
            FROM stock_more sm
            LEFT JOIN stock_info si ON sm.code = si.code
            WHERE sm.float_mv > 0
              AND ({' OR '.join(conds)})"""
        if exclude_st:
            sql += " AND (si.is_st IS NULL OR si.is_st = 0) AND (si.is_quit IS NULL OR si.is_quit = 0)"
        return self.store.con.execute(sql, args).df()

    # ===============================================================
    # 查询
    # ===============================================================
    def get_profile(self, code):
        """获取单票完整基本面画像 (基础+估值+财务宽表)"""
        info = self.store.get_stock_info(code)
        more = self.store.get_stock_more(code)
        fin = self.store.get_financial_wide(code)
        fin_dates = self.store.list_financial_dates(code)
        return {
            "code": code,
            "info": info.to_dict(orient="records")[0] if not info.empty else None,
            "more": more.to_dict(orient="records")[0] if not more.empty else None,
            "financial": fin.tail(12).to_dict(orient="records"),
            "financial_dates": [str(x) for x in fin_dates.get("report_date", [])],
            "financial_fields": len(self.store.con.execute(
                "SELECT DISTINCT field_code FROM financial_facts WHERE code=?",
                [code]).fetchall()),
        }

    def get_financial_wide(self, code, report_date=None, fields=None):
        return self.store.get_financial_wide(code, fields, report_date)

    def get_financial_long(self, code, report_date=None):
        return self.store.get_financial_long(code, report_date)

    def get_summary(self):
        """库概览"""
        return self.store.table_summary()

    def get_chip(self, code, fields=None, limit=300):
        """筹码指标宽表查询"""
        return self.store.get_metric_wide("chip_facts", code, fields, limit)

    def get_chip_range(self, code):
        return self.store.metric_date_range("chip_facts", code)

    def get_l2(self, code, fields=None, limit=300):
        """L2 扩展数据宽表查询"""
        return self.store.get_metric_wide("l2_facts", code, fields, limit)

    def get_l2_range(self, code):
        return self.store.metric_date_range("l2_facts", code)

    def get_shareholder(self, code, holder_type=None, report_date=None):
        """十大股东/十大流通股东明细查询"""
        return self.store.get_shareholder(code, holder_type, report_date)

    def get_shareholder_dates(self, code):
        """已入库的报告期列表 (含财务报告期, 供前端下拉)"""
        return self.store.shareholder_dates(code)

    def get_mainbusi(self, code, report_date=None):
        """主营构成明细查询 (report_date 可选)"""
        return self.store.get_mainbusi(code, report_date)

    def get_mainbusi_dates(self, code):
        """已入库的主营构成报告期列表 (供前端下拉)"""
        return self.store.mainbusi_dates(code)

    def get_mainbusi_profile(self, code):
        """主营构成概述: {product_name, business_desc} 或 None"""
        return self.store.get_mainbusi_profile(code)

    def close(self):
        self.store.close()


def _self_test():
    """联通性自检: 拉取贵州茅台基础信息 + 财务数据"""
    svc = FundamentalService()
    print("客户端连通:", svc.client.ping())
    r = svc.sync_stock_basic(["600519.SH"])
    print("基础信息同步:", r)
    prof = svc.get_profile("600519.SH")
    print("画像: name=", (prof["info"] or {}).get("name"),
          "| 财务报告期数=", len(prof["financial_dates"]),
          "| 财务字段数=", prof["financial_fields"])
    svc.close()


if __name__ == "__main__":
    _self_test()