"""统一缓存组件: TTLCache + CacheBus

取代 quote_service.py / kline_service.py / web_app.py 中三份重复的 _TTLCache。
所有业务代码统一 from cache import TTLCache, CacheBus
"""
from __future__ import annotations
import threading
import time
from typing import Optional


class TTLCache:
    """线程安全 TTL 缓存, 带命中统计与 tag 注册 (供 CacheBus 失效)。

    用法:
        from cache import TTLCache, cache_bus

        c = TTLCache(default_ttl=60, name="sector")
        cache_bus.register(c, tags=["quote.sector"])

        v = c.get(key)
        if v is None:
            v = expensive_compute()
            c.set(key, v)
    """

    _instances: list["TTLCache"] = []

    def __init__(self, default_ttl: int = 60, name: str = ""):
        self._data: dict = {}
        self._ttl = default_ttl
        self._lock = threading.Lock()
        self._name = name or f"TTLCache#{id(self):x}"
        self._tags: list[str] = []
        self._hits = 0
        self._misses = 0
        TTLCache._instances.append(self)

    # ---- 基础操作 ----
    def get(self, key) -> Optional[object]:
        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                self._misses += 1
                return None
            v, ts = entry
            if v is not None and ts > time.time():
                self._hits += 1
                return v
            self._misses += 1
            self._data.pop(key, None)
            return None

    def set(self, key, value, ttl: Optional[int] = None) -> None:
        exp = time.time() + (ttl if ttl is not None else self._ttl)
        with self._lock:
            self._data[key] = (value, exp)

    def invalidate(self, key: Optional[str] = None) -> None:
        with self._lock:
            if key is None:
                self._data.clear()
            else:
                self._data.pop(key, None)

    # ---- 观测 ----
    def info(self) -> dict:
        with self._lock:
            now = time.time()
            size = len(self._data)
            if self._data:
                nearest = min(ts for _, ts in self._data.values())
                next_expiry = max(0, round(nearest - now))
            else:
                next_expiry = -1
        total = self._hits + self._misses
        hit_rate = round(self._hits / total * 100, 1) if total else 0.0
        return {
            "name": self._name,
            "ttl": self._ttl,
            "tags": list(self._tags),
            "size": size,
            "hits": self._hits,
            "misses": self._misses,
            "hit_rate_pct": hit_rate,
            "next_expiry_sec": next_expiry,
        }


class CacheBus:
    """缓存失效总线: 按 tag 批量失效一组 TTLCache。

    用法:
        cache_bus.register(cache_instance, tags=["quote.sector", "kline"])
        cache_bus.invalidate_tags("kline")   # 清掉所有打了 "kline" 标签的缓存

    典型场景: 数据同步完成后清掉相关业务域的缓存。
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._tag_to_caches: dict[str, list[TTLCache]] = {}

    def register(self, cache: TTLCache, tags: list[str]) -> None:
        with self._lock:
            cache._tags = list(tags)
            for t in tags:
                self._tag_to_caches.setdefault(t, []).append(cache)

    def invalidate_tags(self, *tags: str) -> int:
        """失效所有匹配 tag 的缓存, 返回清掉的缓存数量。"""
        targets: set[TTLCache] = set()
        with self._lock:
            for t in tags:
                for c in self._tag_to_caches.get(t, []):
                    targets.add(c)
        for c in targets:
            c.invalidate()
        return len(targets)

    def all_tags(self) -> list[str]:
        with self._lock:
            return list(self._tag_to_caches.keys())


# ---- 单例 ----
cache_bus = CacheBus()


# ---- 便捷函数 ----
def all_cache_info() -> list[dict]:
    """返回所有 TTLCache 实例的观测信息 (供 /api/cache/debug 使用)。"""
    return [c.info() for c in TTLCache._instances]
