# Last modified: 2026-08-13 00:38:32
"""
TQ-Local HTTP 客户端 —— 通达信本地量化数据服务封装
=====================================================
通过本机 HTTP JSON-RPC 服务 (http://127.0.0.1:17709/) 调用 tqcenter 接口,
与现有 tqcenter.py 直连方式解耦, 供上层业务 (基本面/筹码/分析) 统一复用。

设计要点:
- 高内聚低耦合: 本模块只做 JSON-RPC 传输 + 接口映射, 不含业务逻辑
- 所有 TQ 数据源接口集中归拢于此, 便于后续迁移/扩展数据源
- 线程安全: 请求带自增 id, 可并发调用

用法:
    from tdx_tq_client import TQLocalClient
    tq = TQLocalClient()
    tq.get_stock_info("600519.SH")
"""
import json
import time
import urllib.request
import urllib.error
import threading

from config import TQ_URL, TQ_TIMEOUT, TQ_MAX_RETRY
from logger import get_logger
_log = get_logger("tdx_tq_client")


class TQError(Exception):
    """TQ 接口调用错误"""
    pass


class TQLocalClient:
    """通达信 TQ-Local HTTP JSON-RPC 客户端"""

    DEFAULT_URL = TQ_URL

    def __init__(self, url=None, timeout=None, max_retry=None):
        self.url = url or self.DEFAULT_URL
        self.timeout = timeout if timeout is not None else TQ_TIMEOUT
        self.max_retry = max_retry if max_retry is not None else TQ_MAX_RETRY
        self._lock = threading.Lock()
        self._seq = 0

    # ---------------------------------------------------------------
    # 底层传输
    # ---------------------------------------------------------------
    def _next_id(self):
        with self._lock:
            self._seq += 1
            return self._seq

    def call(self, method, params=None, retry=None):
        """发送 JSON-RPC 请求并返回 result 内容 (ErrorId/Value 已解包)

        params: dict 对应 tqcenter 接口入参 (如 {'stock_code': '600519.SH'})
        返回: 底层 result 对象; ErrorId == "0" 时正常返回 Value,
              否则抛 TQError(携带 ErrorId 与错误信息)
        """
        params = params or {}
        params = {k: v for k, v in params.items() if v is not None}
        payload = {"id": self._next_id(), "method": method, "params": params}
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        retry = self.max_retry if retry is None else retry
        _t0 = time.perf_counter()
        _log.debug("→ %s params=%s", method, params if len(json.dumps(params)) < 400 else "{...}")

        last_err = None
        for attempt in range(retry + 1):
            try:
                req = urllib.request.Request(
                    self.url, data=body,
                    headers={"Content-Type": "application/json; charset=utf-8"},
                    method="POST")
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    raw = json.loads(resp.read().decode("utf-8"))
                if "error" in raw and raw["error"]:
                    raise TQError(f"[{method}] JSON-RPC error: {raw['error']}")
                result = raw.get("result", {})
                if isinstance(result, dict) and "ErrorId" in result:
                    err_id = result.get("ErrorId")
                    if str(err_id) != "0":
                        raise TQError(f"[{method}] ErrorId={err_id} msg={result.get('Value')}")
                _dt = time.perf_counter() - _t0
                _log.debug("← %s ok (%.0fms)", method, _dt * 1000)
                return result
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                last_err = e
                _log.warning("× %s attempt %d/%d: %s", method, attempt + 1, retry + 1, e)
                continue
            except TQError as e:
                _dt = time.perf_counter() - _t0
                _log.error("✗ %s fail (%.0fms): %s", method, _dt * 1000, e)
                raise
            except json.JSONDecodeError as e:
                raise TQError(f"[{method}] JSON 解析失败: {e}")
        _dt = time.perf_counter() - _t0
        _log.error("✗ %s 连接失败 (%.0fms, %d次): %s", method, _dt * 1000, retry + 1, last_err)
        raise TQError(f"[{method}] 连接失败({retry+1}次): {last_err}")

    # ---------------------------------------------------------------
    # 基础资料类
    # ---------------------------------------------------------------
    @staticmethod
    def _unwrap(result):
        """解包底层返回: {ErrorId, Value} -> Value; 扁平结构去掉 ErrorId"""
        if isinstance(result, dict):
            if "Value" in result:
                return result["Value"]
            result = {k: v for k, v in result.items() if k != "ErrorId"}
        return result

    def get_stock_info(self, stock_code, field_list=None):
        """证券基础信息 (名称/行业/股本/财务快照等)"""
        return self._unwrap(self.call("get_stock_info", {
            "stock_code": stock_code,
            "field_list": field_list or []}))

    def get_more_info(self, stock_code, field_list=None):
        """更多证券信息 (估值/涨幅/资金流/涨跌停等)"""
        return self._unwrap(self.call("get_more_info", {
            "stock_code": stock_code,
            "field_list": field_list or []}))

    def get_match_stkinfo(self, key_word):
        """按名称/拼音/代码模糊查证券"""
        return self._unwrap(self.call("get_match_stkinfo", {"key_word": key_word}))

    def get_stock_list(self, market="5", list_type=1):
        """系统证券列表; market=5 全部A股, list_type=1 带名称"""
        return self._unwrap(self.call("get_stock_list", {
            "market": str(market), "list_type": list_type}))

    def get_sector_list(self, list_type=1):
        """板块列表"""
        return self._unwrap(self.call("get_sector_list", {"list_type": list_type}))

    def get_stock_list_in_sector(self, block_code, block_type=0, list_type=1):
        """板块成分股"""
        return self._unwrap(self.call("get_stock_list_in_sector", {
            "block_code": block_code, "block_type": block_type,
            "list_type": list_type}))

    def get_relation(self, stock_code):
        """证券所属板块/关联品种"""
        return self._unwrap(self.call("get_relation", {"stock_code": stock_code}))

    def get_gb_info(self, stock_code, date_list, count):
        """股本信息 (总股本/流通股本)"""
        return self._unwrap(self.call("get_gb_info", {
            "stock_code": stock_code, "date_list": date_list, "count": count}))

    def get_gb_info_by_date(self, stock_code, start_date, end_date):
        """按日期区间取股本信息"""
        return self._unwrap(self.call("get_gb_info_by_date", {
            "stock_code": stock_code, "start_date": start_date,
            "end_date": end_date}))

    # ---------------------------------------------------------------
    # 行情类
    # ---------------------------------------------------------------
    def get_market_data(self, stock_list, period="1d", count=0,
                        dividend_type="none", field_list=None,
                        start_time=None, end_time=None):
        """K线/历史行情 → 返回 {code: {field: [...]}} 格式"""
        return self._unwrap(self.call("get_market_data", {
            "stock_list": stock_list, "period": period, "count": count,
            "dividend_type": dividend_type,
            "field_list": field_list or [],
            "start_time": start_time, "end_time": end_time}))

    def price_df(self, raw, field, column_names=None):
        """从 get_market_data 返回中抽取某字段 → DataFrame

        自动兼容三种输入格式:
          1) 已 unwrap (推荐, get_market_data 现在返回这种):
             raw = {code: {Date:[], Open:[], Close:[], ...}}
          2) 未 unwrap (包含 ErrorId):
             raw = {Value: {code: {Date:[], ...}}, ErrorId: 0}
          3) 原生 tqcenter.tq 扁平格式:
             raw = {Open: DataFrame, Close: DataFrame, ...}

        返回: DataFrame(index=Date, columns=column_names 或 code)
        """
        import pandas as pd
        if not isinstance(raw, dict) or not raw:
            return pd.DataFrame()

        # 格式 2) 未 unwrap: 有 "Value" 且值是 dict
        if "Value" in raw and isinstance(raw.get("Value"), dict):
            value_dict = raw["Value"]
        # 格式 3) 原生 tqcenter 扁平: 直接 field 是 DataFrame
        elif field in raw and isinstance(raw.get(field), (pd.DataFrame, pd.Series)):
            return raw[field]
        # 格式 1) 已 unwrap: 直接把 raw 当作 {code: {...}}
        else:
            value_dict = raw

        frames = []
        for code, inner in value_dict.items():
            if not isinstance(inner, dict):
                continue
            dates = inner.get("Date", [])
            values = inner.get(field, [])
            if not dates or not values:
                continue
            col_name = (column_names[list(value_dict.keys()).index(code)]
                        if column_names and list(value_dict.keys()).index(code) < len(column_names)
                        else code)
            df = pd.DataFrame({col_name: pd.to_numeric(values, errors="coerce")},
                              index=pd.to_datetime([str(d) for d in dates]))
            frames.append(df)
        if not frames:
            return pd.DataFrame()
        return pd.concat(frames, axis=1)

    def get_market_snapshot(self, stock_code, field_list=None):
        """实时行情快照"""
        return self.call("get_market_snapshot", {
            "stock_code": stock_code, "field_list": field_list or []})

    def get_pricevol(self, stock_list):
        """价量数据 (前收盘/现价/成交量)"""
        return self.call("get_pricevol", {"stock_list": stock_list})

    def get_exday_data(self, stock_code, count=1):
        """扩展日线 L2 数据"""
        return self.call("get_exday_data", {
            "stock_code": stock_code, "count": count})

    def get_zdt_data(self, stock_list):
        """涨跌停数据"""
        return self.call("get_zdt_data", {"stock_list": stock_list})

    def get_divid_factors(self, stock_code, start_time=None, end_time=None):
        """除权除息数据"""
        return self.call("get_divid_factors", {
            "stock_code": stock_code,
            "start_time": start_time, "end_time": end_time})

    # ---------------------------------------------------------------
    # 专业数据 (基本面核心)
    # ---------------------------------------------------------------
    def get_financial_data(self, stock_list, field_list, start_time=None,
                           end_time=None, report_type="report_time"):
        """专业财务序列数据 (FN1~FN584)

        注意: HTTP 层将 params 直接透传底层 batch_json, 字段参数名必须为
        table_list (而非 field_list); 返回为列式 {code: {FN1:[..], tag_time:[..]}}
        结构, 当 ProDataPaged=True 时按 stock_page_index 自动翻页拼接。
        """
        params = {
            "stock_list": stock_list,
            "table_list": list(field_list),
            "start_time": start_time or "",
            "end_time": end_time or "",
            "report_type": report_type,
        }
        result = self.call("get_financial_data", params)
        # 分页拼接 (底层服务带 ProDataPaged 时)
        if isinstance(result, dict) and result.get("ProDataPaged"):
            original_value = result.get("Value")
            merged_value = original_value if isinstance(original_value, dict) else {}
            stock_total_pages = int(result.get("stock_total_pages", 1) or 1)
            failed = 0
            for page_idx in range(1, stock_total_pages):
                p_params = dict(params)
                p_params["stock_page_index"] = page_idx
                try:
                    page = self.call("get_financial_data", p_params)
                except Exception as e:
                    failed += 1
                    _log.warning(
                        "get_financial_data 分页第 %d/%d 页拉取失败: %s",
                        page_idx + 1, stock_total_pages, e)
                    continue
                page_value = page.get("Value") if isinstance(page, dict) else None
                if isinstance(page_value, dict):
                    merged_value.update(page_value)
            if failed:
                _log.warning(
                    "get_financial_data 分页共 %d/%d 页失败, 数据可能不完整",
                    failed, stock_total_pages)
            # 有分页数据时用合并结果; 否则保留原始返回, 不再用空 dict 覆盖有效 Value
            if merged_value:
                result = dict(result)
                result["Value"] = merged_value
        return result

    def get_financial_data_by_date(self, stock_list, field_list,
                                   year=0, mmdd=0):
        """按年度/季度获取财务数据 (参数名同样为 table_list)"""
        return self.call("get_financial_data_by_date", {
            "stock_list": stock_list, "table_list": list(field_list),
            "year": year, "mmdd": mmdd})

    def get_gpjy_value(self, stock_list, field_list, start_time=None,
                       end_time=None):
        """股票交易类专业序列 (GP1~GP46) 注意: 参数名为 table_list"""
        return self._unwrap(self.call("get_gpjy_value", {
            "stock_list": stock_list, "table_list": list(field_list),
            "start_time": start_time or "", "end_time": end_time or ""}))

    def get_gpjy_value_by_date(self, stock_list, field_list, year=0, mmdd=0):
        """按日期获取股票交易类专业数据"""
        return self._unwrap(self.call("get_gpjy_value_by_date", {
            "stock_list": stock_list, "table_list": list(field_list),
            "year": year, "mmdd": mmdd}))

    def get_gp_one_data(self, stock_list, field_list):
        """单股/多股一次性专业数据 (GO1~GO47) 注意: 参数名为 table_list"""
        return self.call("get_gp_one_data", {
            "stock_list": stock_list, "table_list": list(field_list)})

    # ---------------------------------------------------------------
    # 公式接口 (筹码指标等)
    # ---------------------------------------------------------------
    def formula_process_mul_zb(self, formula_name, formula_arg="",
                               xsflag=-1, return_count=1, return_date=False,
                               stock_list=None, stock_period="1d",
                               start_time=None, end_time=None, count=0,
                               dividend_type=0):
        """批量调用通达信指标公式 (无需提前设置数据, 可多股)

        用于获取筹码类指标: MCST/CYS/ASR/SCR/CYC 等。
        返回: {code: {指标: [{'Date':..,'Value':..}, ...], ...}}
        """
        params = {
            "formula_name": formula_name,
            "formula_arg": str(formula_arg),
            "xsflag": xsflag,
            "return_count": int(return_count),
            "return_date": bool(return_date),
            "stock_list": stock_list or [],
            "stock_period": stock_period,
            "start_time": start_time or "",
            "end_time": end_time or "",
            "count": int(count),
            "dividend_type": int(dividend_type),
        }
        return self._unwrap(self.call("formula_process_mul_zb", params))

    def formula_process_mul_xg(self, formula_name, formula_arg="",
                               return_count=1, return_date=False,
                               stock_list=None, stock_period="1d",
                               start_time=None, end_time=None, count=0,
                               dividend_type=0):
        """批量调用通达信选股公式"""
        params = {
            "formula_name": formula_name,
            "formula_arg": str(formula_arg),
            "return_count": int(return_count),
            "return_date": bool(return_date),
            "stock_list": stock_list or [],
            "stock_period": stock_period,
            "start_time": start_time or "",
            "end_time": end_time or "",
            "count": int(count),
            "dividend_type": int(dividend_type),
        }
        return self._unwrap(self.call("formula_process_mul_xg", params))

    def formula_get_all(self, formula_type=0):
        """公式列表: 0指标 1选股 2专家"""
        return self._unwrap(self.call("formula_get_all", {"formula_type": formula_type}))

    def formula_get_info(self, formula_type=0, formula_code=""):
        """公式详情"""
        return self._unwrap(self.call("formula_get_info", {
            "formula_type": formula_type, "formula_code": formula_code}))

    # ---------------------------------------------------------------
    # 刷新/交互
    # ---------------------------------------------------------------
    def refresh_cache(self, market="AG", force=False):
        return self.call("refresh_cache", {"market": market, "force": force})

    def refresh_kline(self, stock_list, period="1d"):
        return self.call("refresh_kline", {
            "stock_list": stock_list, "period": period})

    def download_file(self, stock_code=None, down_time=None, down_type=1):
        """下载文件（十大股东/ETF申赎/舆情/综合信息）

        down_type: 1=十大股东(down_time只生效年份) 2=ETF申赎清单
                   3=最近舆情 4=综合信息文件
        返回: 底层 result (含 Msg 提示); 文件落盘到客户端 PYPlugins/data 目录
        """
        return self.call("download_file", {
            "stock_code": stock_code, "down_time": down_time,
            "down_type": down_type})

    def read_holders_file(self, stock_code, year, data_dir=None):
        """读取客户端落盘的十大股东 JSON 文件

        命名规则: {data_dir}/holders{code}_{year}.json
        data_dir: 客户端 PYPlugins/data 目录; 缺省探测常见路径
        返回: 解析后的 Python 对象 (list[{'gdxx': str}]) 或 None
        """
        import glob
        import os
        from pathlib import Path
        code = str(stock_code).split(".")[0]
        candidates = []
        if data_dir:
            candidates.append(str(data_dir))
        # 常见客户端安装路径探测 (soft-mock 模拟版 / 标准版)
        env_home = os.environ.get("TDX_HOME") or os.environ.get("TDX_PATH")
        for base in ([env_home] if env_home else []) + \
                ["F:/TDX", "D:/TDX", "E:/TDX", "C:/new_tdx",
                 str(Path(__file__).resolve().parent.parent)]:
            for sub in ("soft-mock", "vipdoc"):
                cand = Path(base) / sub / "PYPlugins" / "data"
                if cand.exists():
                    candidates.append(str(cand))
            cand = Path(base) / "PYPlugins" / "data"
            if cand.exists():
                candidates.append(str(cand))
        for cand in candidates:
            pattern = str(Path(cand) / f"holders{code}_{year}.json")
            matches = glob.glob(pattern)
            if matches:
                with open(matches[0], "r", encoding="utf-8") as f:
                    return json.load(f)
        return None

    def read_mainbusi_file(self, stock_code, year, data_dir=None):
        """读取客户端落盘的主营构成 JSON 文件 (download_file down_type=5)

        命名规则: {data_dir}/mainbusi{code}_{year}.json
        返回: 解析后的 Python 对象 (list[{'zygc': str}]) 或 None
        """
        import glob
        import os
        from pathlib import Path
        code = str(stock_code).split(".")[0]
        candidates = []
        if data_dir:
            candidates.append(str(data_dir))
        env_home = os.environ.get("TDX_HOME") or os.environ.get("TDX_PATH")
        for base in ([env_home] if env_home else []) + \
                ["F:/TDX", "D:/TDX", "E:/TDX", "C:/new_tdx",
                 str(Path(__file__).resolve().parent.parent)]:
            for sub in ("soft-mock", "vipdoc"):
                cand = Path(base) / sub / "PYPlugins" / "data"
                if cand.exists():
                    candidates.append(str(cand))
            cand = Path(base) / "PYPlugins" / "data"
            if cand.exists():
                candidates.append(str(cand))
        for cand in candidates:
            pattern = str(Path(cand) / f"mainbusi{code}_{year}.json")
            matches = glob.glob(pattern)
            if matches:
                with open(matches[0], "r", encoding="utf-8") as f:
                    return json.load(f)
        return None

    def exec_to_tdx(self, url):
        return self.call("exec_to_tdx", {"url": url})

    # ---------------------------------------------------------------
    # 工具
    # ---------------------------------------------------------------
    def ping(self):
        """连通性测试"""
        try:
            r = self.get_match_stkinfo("茅台")
            return bool(r)
        except Exception:
            return False

    def __repr__(self):
        return f"<TQLocalClient url={self.url} connected={self.ping()}>"


# 模块级单例 (默认使用)
_default = None


def get_client(url=None):
    """获取全局单例客户端 (惰性创建)"""
    global _default
    if _default is None:
        _default = TQLocalClient(url=url)
    return _default
