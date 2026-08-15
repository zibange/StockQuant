# Last modified: 2026-08-14 01:10:00
"""
QuoteService — 行情查询服务
职责: 单票实时行情 + 板块排行 + 周期收益率计算

性能优化:
  - sector: TTL=60s 内存缓存 + ThreadPoolExecutor 并行快照
"""
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from logger import get_logger
from cache import TTLCache, cache_bus
_log = get_logger("quote_service")


class QuoteService:
    """行情查询 (单票快照 / 板块排行 / 周期收益)

    依赖注入:
      tq_client — TQLocalClient, 通达信行情接口
    """

    _PERIOD_COUNTS = {"5d": 5, "10d": 10, "20d": 20, "60d": 60, "ytd": 250}

    def __init__(self, tq_client=None):
        self.tq = tq_client
        self._sector_cache = TTLCache(default_ttl=60, name="quote.sector")
        cache_bus.register(self._sector_cache, tags=["quote.sector"])
        self._executor = ThreadPoolExecutor(max_workers=16, thread_name_prefix="sector")

    @classmethod
    def _period_count(cls, period):
        return cls._PERIOD_COUNTS.get(period, 1)

    # ================================================================
    # 单票实时行情
    # ================================================================
    def quote(self, code):
        """获取单票实时快照 + 股票名 (无缓存, 追求实时)"""
        if self.tq is None:
            raise RuntimeError("QuoteService 缺少依赖: tq_client 未注入")
        snap = self.tq.get_market_snapshot(stock_code=code) or {}
        info = self.tq.get_stock_info(stock_code=code) or {}
        return {
            "code": code,
            "name": info.get("Name") or code,
            "price": float(snap.get("Now") or snap.get("LastClose") or 0),
            "last_close": float(snap.get("LastClose") or 0),
        }

    # ================================================================
    # 板块排行
    # ================================================================
    @staticmethod
    def _normalize_codes(all_sectors):
        """TQLocalClient 返回 [{Code,Name}] dict 列表, 原生 SDK 返回 str 列表, 统一转 str"""
        out = []
        for item in all_sectors:
            if isinstance(item, dict):
                code = item.get("Code") or item.get("code") or item.get("SectorCode")
                if code:
                    out.append(code)
            elif isinstance(item, str):
                out.append(item)
        return out

    def _filter_sector_codes(self, all_sectors, sector_type):
        codes = self._normalize_codes(all_sectors)
        if sector_type == "industry":
            return [s for s in codes if s.startswith("881") or s.startswith("882")]
        elif sector_type == "concept":
            return [s for s in codes if s.startswith("8805") or s.startswith("8806")
                    or s.startswith("8807") or s.startswith("8808") or s.startswith("8809")]
        elif sector_type == "regional":
            return [s for s in codes if s.startswith("8802")]
        return codes

    def _fetch_one_sector(self, s, period):
        """单个板块快照 (在线程池里执行)"""
        try:
            info = self.tq.get_stock_info(stock_code=s) or {}
            name = info.get("Name") or info.get("name") or s
            snap = self.tq.get_market_snapshot(stock_code=s) or {}
            now = float(snap.get("Now") or 0)
            last_close = float(snap.get("LastClose") or 0)
            zangsu = float(snap.get("Zangsu") or 0)
            up = int(snap.get("UpHome") or 0)
            down = int(snap.get("DownHome") or 0)
            items = int(snap.get("ItemNum") or 0)
            volume = float(snap.get("Volume") or 0)
            amount = float(snap.get("Amount") or 0)
            if now <= 0 and last_close <= 0:
                return None
            row = {
                "code": s, "name": name,
                "now": round(now, 2),
                "last_close": round(last_close, 2),
                "zangsu": round(zangsu, 2),
                "up": up, "down": down, "items": items,
                "volume": int(volume),
                "amount": round(amount, 2),
            }
            if period != "1d" and last_close > 0:
                try:
                    raw = self.tq.get_market_data(
                        field_list=[], stock_list=[s], period="1d",
                        count=self._period_count(period), dividend_type="front")
                    if raw is not None and "Close" in raw and "error" not in raw:
                        pdf = self.tq.price_df(raw, "Close", column_names=[s])
                        if len(pdf) >= 2:
                            past_close = float(pdf.iloc[0][s])
                            if past_close > 0:
                                row["ret_period"] = round((now - past_close) / past_close * 100, 2)
                                row["past_close"] = round(past_close, 2)
                except Exception:
                    pass
            return row
        except Exception:
            return None

    def sector(self, sector_type="industry", period="1d"):
        """行业/概念/地区板块行情排行 (TTL=60s 缓存 + 并行快照)"""
        if self.tq is None:
            raise RuntimeError("QuoteService 缺少依赖: tq_client 未注入")

        cache_key = f"{sector_type}|{period}"
        cached = self._sector_cache.get(cache_key)
        if cached is not None:
            return cached

        t0 = time.time()
        all_sectors = self.tq.get_sector_list() or []
        codes = self._filter_sector_codes(all_sectors, sector_type)

        results = []
        futures = {self._executor.submit(self._fetch_one_sector, s, period): s for s in codes}
        for fut in as_completed(futures):
            row = fut.result()
            if row is not None:
                results.append(row)

        results.sort(key=lambda x: x.get("ret_period", x["zangsu"]), reverse=True)
        out = {"type": sector_type, "period": period, "count": len(results), "list": results}
        self._sector_cache.set(cache_key, out)
        _log.info("sector(%s,%s): %d items in %.0fms (cached TTL=60s)",
                 sector_type, period, len(results), (time.time() - t0) * 1000)
        return out
