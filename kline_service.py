# Last modified: 2026-08-14 01:12:00
"""
KlineService — K 线行情查询 + 技术指标计算 + 估值评分
职责: 组合 KlineStore(存储) + TQLocalClient(行情), 输出完整 K 线 JSON

性能优化:
  - get_kline: TTL=120s 内存缓存 (key=code|period|n|dividend|start|end)
    force=True 时跳过缓存
"""
import time

import pandas as pd
from logger import get_logger
from cache import TTLCache, cache_bus
_log = get_logger("kline_service")


class KlineService:
    """K 线行情查询 (带技术指标 + 估值评分)

    依赖注入:
      kl_store  — KlineStore, 负责本地/远程 K 线数据获取
      tq_client — TQLocalClient, 通达信行情接口 (快照 + 更多信息)
    """

    def __init__(self, kl_store=None, tq_client=None):
        self.kl_store = kl_store
        self.tq = tq_client
        self._cache = TTLCache(default_ttl=120, name="kline")
        cache_bus.register(self._cache, tags=["kline"])

    # ================================================================
    # 主入口: get_kline
    # ================================================================
    def get_kline(self, code, period="1d", n=180, dividend="front",
                  force=False, start=None, end=None):
        """完整 K 线查询 (指标 + 估值 + 快照, TTL=120s 缓存)"""
        if self.kl_store is None:
            raise RuntimeError("KlineService 缺少依赖: kl_store 未注入")

        if not force:
            cache_key = f"{code}|{period}|{n}|{dividend}|{start or ''}|{end or ''}"
            cached = self._cache.get(cache_key)
            if cached is not None:
                return cached

        result = self._do_get_kline(code, period, n, dividend, force, start, end)

        if not force and not result.get("_empty"):
            cache_key = f"{code}|{period}|{n}|{dividend}|{start or ''}|{end or ''}"
            self._cache.set(cache_key, result)

        return result

    def _do_get_kline(self, code, period, n, dividend, force, start, end):

        local_has = self.kl_store.has_data(code, period=period)
        local_range = self.kl_store.get_date_range(code, period=period) if local_has else None

        need_remote = force or not local_has
        if need_remote:
            try:
                self._fetch_from_tq(code, period, dividend)
            except Exception as e:
                if not local_has:
                    raise
                _log.warning("TQ fetch failed, fallback to local: %s", e)

        df = self.kl_store.load(code, period=period)
        avail_min = df.index.min()
        avail_max = df.index.max()

        if start or end:
            mask = pd.Series(True, index=df.index)
            if start:
                mask &= df.index >= pd.to_datetime(start)
            if end:
                mask &= df.index <= pd.to_datetime(end)
            filtered = df[mask]
            if not filtered.empty:
                df = filtered
            elif local_has and not df.empty:
                df = df
            else:
                df = filtered
        df = df.tail(n)

        if df.empty:
            return {
                "code": code, "count": 0, "data": [], "latest": None,
                "available_range": self._date_range(avail_min, avail_max),
                "hint": f"通达信数据源当前仅支持 {self._fmt_date(avail_min)} 之后的数据，所选日期范围暂无数据",
                "_empty": True,
            }

        rows = self._compute_indicators(df)
        snap, more, info = self._fetch_realtime(code)

        return {
            "code": code, "count": len(rows),
            "available_range": self._date_range(avail_min, avail_max),
            "latest": rows[-1] if rows else None,
            "snap": self._format_snap(snap),
            "more": self._format_more(code, snap, more, info),
            "data": rows,
        }

    # ================================================================
    # 辅助: 远程拉取
    # ================================================================
    def _fetch_from_tq(self, code, period, dividend):
        """从通达信拉取 K 线并 upsert 到本地"""
        if self.tq is None:
            raise RuntimeError("KlineService 缺少依赖: tq_client 未注入")
        kw = dict(field_list=[], stock_list=[code], period=period,
                  dividend_type=dividend, count=24000)
        raw = self.tq.get_market_data(**kw)
        fields = ["Open", "High", "Low", "Close", "Volume", "Amount"]
        parts = []
        for f in fields:
            sub = self.tq.price_df(raw, f, column_names=[code])
            if sub.empty:
                continue
            sub.columns = [f]
            parts.append(sub)
        df = pd.concat(parts, axis=1) if parts else pd.DataFrame()
        if df.empty:
            raise ValueError(f"无法获取 {code}")
        df.index = pd.to_datetime(df.index.astype(str))
        self.kl_store.upsert(code, df.reset_index(), period=period, dividend_type=dividend)
        return df

    # ================================================================
    # 辅助: 技术指标计算
    # ================================================================
    def _compute_indicators(self, df):
        """计算 MA5/10/20/60 + MACD + KDJ, 输出 list[dict]"""
        close = df["Close"]
        ma5  = close.rolling(5).mean()
        ma10 = close.rolling(10).mean()
        ma20 = close.rolling(20).mean()
        ma60 = close.rolling(60).mean() if len(close) >= 60 else pd.Series([None]*len(close), index=close.index)

        ema12 = close.ewm(span=12, adjust=False).mean()
        ema26 = close.ewm(span=26, adjust=False).mean()
        dif = ema12 - ema26
        dea = dif.ewm(span=9, adjust=False).mean()
        macd = (dif - dea) * 2

        hh9 = df["High"].rolling(9).max()
        ll9 = df["Low"].rolling(9).min()
        rsv = (close - ll9) / (hh9 - ll9) * 100
        rsv = rsv.fillna(50)
        k = rsv.ewm(alpha=1/3, adjust=False).mean()
        d = k.ewm(alpha=1/3, adjust=False).mean()
        j = 3 * k - 2 * d

        rows = []
        for (dt, r), m5, m10, m20, m60_, d_dif, d_dea, d_macd, k_, d_, j_ in zip(
            df.iterrows(), ma5.values, ma10.values, ma20.values, ma60.values,
            dif.values, dea.values, macd.values,
            k.values, d.values, j.values):
            rows.append({
                "date": dt.strftime("%Y-%m-%d"),
                "open":  round(float(r["Open"]),  2),
                "high":  round(float(r["High"]),  2),
                "low":   round(float(r["Low"]),   2),
                "close": round(float(r["Close"]), 2),
                "volume": int(r["Volume"]),
                "amount": round(float(r["Amount"]), 2),
                "ma5":  round(float(m5),  2) if pd.notna(m5)  else None,
                "ma10": round(float(m10), 2) if pd.notna(m10) else None,
                "ma20": round(float(m20), 2) if pd.notna(m20) else None,
                "ma60": round(float(m60_),2) if pd.notna(m60_) else None,
                "dif":  round(float(d_dif),  4) if pd.notna(d_dif)  else None,
                "dea":  round(float(d_dea),  4) if pd.notna(d_dea)  else None,
                "macd": round(float(d_macd), 4) if pd.notna(d_macd) else None,
                "k":    round(float(k_), 4) if pd.notna(k_) else None,
                "d":    round(float(d_), 4) if pd.notna(d_) else None,
                "j":    round(float(j_), 4) if pd.notna(j_) else None,
            })
        return rows

    # ================================================================
    # 辅助: 实时行情快照 + 更多信息
    # ================================================================
    def _fetch_realtime(self, code):
        """并行拉取 snap / more / info, 失败降级为空 dict"""
        snap = {}
        more = {}
        info = {}
        if self.tq is not None:
            try: snap = self.tq.get_market_snapshot(stock_code=code) or {}
            except Exception: pass
            try: more = self.tq.get_more_info(stock_code=code) or {}
            except Exception: pass
            try: info = self.tq.get_stock_info(stock_code=code) or {}
            except Exception: pass
        return snap, more, info

    def _format_snap(self, snap):
        return {
            "now": snap.get("Now"), "preclose": snap.get("LastClose"),
            "open": snap.get("Open"), "high": snap.get("Max"),
            "low": snap.get("Min"), "vol": snap.get("Volume"),
            "amount": snap.get("Amount"),
        }

    def _format_more(self, code, snap, more, info):
        """估值评分 + 个股详细信息"""
        pe_val = more.get("DynaPE") or more.get("MorePE") or more.get("StaticPE_TTM")
        pb_val = more.get("PB_MRQ")
        zsz_val = more.get("Zsz")
        his_high = more.get("HisHigh")
        his_low = more.get("HisLow")
        beta_val = more.get("BetaValue")
        dy_ratio = more.get("DYRatio")
        zaf_val = more.get("ZAF")
        hsl_val = more.get("fHSL")
        liab_val = more.get("fLianB")
        ever_zt = more.get("EverZTCount") or "0"
        liab = hsl_val and float(hsl_val) * 100
        now_price = float(snap.get("Now") or snap.get("LastClose") or 0) or None
        mid_high = None
        if his_high and his_low and now_price:
            mid_high = round((now_price - float(his_low)) / (float(his_high) - float(his_low)) * 100, 1)

        pe_info = self._score_pe(pe_val)
        pb_info = self._score_pb(pb_val)
        beta_info = self._score_beta(beta_val)
        dy_info = self._score_dy(dy_ratio)
        hsl_info = self._score_hsl(hsl_val)

        return {
            "name": info.get("Name") or more.get("Name"),
            "industry": more.get("rs_hyname") or info.get("rs_hyname") or "-",
            "pe": pe_val, "pe_label": pe_info[0], "pe_color": pe_info[1], "pe_tip": pe_info[2],
            "pb": pb_val, "pb_label": pb_info[0], "pb_color": pb_info[1], "pb_tip": pb_info[2],
            "zsz": zsz_val, "ltsz": more.get("Ltsz"),
            "zaf": zaf_val, "hsl": hsl_val,
            "hsl_label": hsl_info[0], "hsl_color": hsl_info[1], "hsl_tip": hsl_info[2],
            "liab": liab_val,
            "beta": beta_val, "beta_label": beta_info[0], "beta_color": beta_info[1], "beta_tip": beta_info[2],
            "his_high": his_high, "his_low": his_low,
            "mid_high_pct": mid_high,
            "dy_ratio": dy_ratio, "dy_label": dy_info[0], "dy_color": dy_info[1], "dy_tip": dy_info[2],
            "ever_zt": ever_zt,
        }

    # ================================================================
    # 估值评分系统 (PE/PB/Beta/股息率/换手率)
    # ================================================================
    @staticmethod
    def _score_pe(pe):
        try:
            pe = float(pe)
            if pe <= 0: return ("亏损", "#dc2626", "公司亏损无PE")
            if pe < 15: return ("低估", "#16a34a", "PE < 15，估值偏低，价值洼地")
            if pe < 25: return ("合理", "#2563eb", "15~25 为成熟股合理区间")
            if pe < 40: return ("偏高", "#f59e0b", "25~40 成长溢价需验证")
            return ("高估", "#dc2626", "PE > 40，泡沫风险")
        except: return ("-", "#94a3b8", "数据缺失")

    @staticmethod
    def _score_pb(pb):
        try:
            pb = float(pb)
            if pb <= 1: return ("破净", "#dc2626", "PB <= 1 资产折价，警惕价值陷阱")
            if pb < 2: return ("低PB", "#16a34a", "PB 1~2 价值股区间")
            if pb < 5: return ("合理", "#2563eb", "PB 2~5 优质成长区间")
            return ("高估", "#dc2626", "PB > 5 泡沫化")
        except: return ("-", "#94a3b8", "数据缺失")

    @staticmethod
    def _score_beta(b):
        try:
            b = float(b)
            if b < 0.8: return ("低波", "#16a34a", "Beta<0.8，波动低于大盘")
            if b <= 1.2: return ("同步", "#2563eb", "0.8~1.2 与大盘同频")
            if b <= 1.8: return ("高波", "#f59e0b", "1.2~1.8 进攻型")
            return ("激进", "#dc2626", ">1.8 超高波动")
        except: return ("-", "#94a3b8", "数据缺失")

    @staticmethod
    def _score_dy(dy):
        try:
            dy = float(dy)
            if dy >= 4: return ("高息", "#16a34a", "股息率>=4% 具有吸引力")
            if dy >= 2: return ("普通", "#2563eb", "2%~4% 正常分红")
            if dy > 0: return ("偏低", "#f59e0b", "<2% 分红少")
            return ("-", "#94a3b8", "无股息")
        except: return ("-", "#94a3b8", "数据缺失")

    @staticmethod
    def _score_hsl(hsl):
        try:
            hsl = float(hsl)
            if hsl < 0.5: return ("低迷", "#94a3b8", "换手率<0.5% 流动性差")
            if hsl < 2: return ("活跃", "#2563eb", "0.5~2% 正常交易")
            if hsl < 5: return ("热门", "#16a34a", "2~5% 资金关注")
            return ("亢奋", "#dc2626", ">5% 追高风险")
        except: return ("-", "#94a3b8", "数据缺失")

    # ================================================================
    # 辅助: 日期格式化
    # ================================================================
    @staticmethod
    def _fmt_date(d):
        return str(d.date()) if hasattr(d, "date") else str(d)

    @classmethod
    def _date_range(cls, dmin, dmax):
        return {"min": cls._fmt_date(dmin), "max": cls._fmt_date(dmax)}
