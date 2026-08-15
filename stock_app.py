# Last modified: 2026-08-12 22:05:00
"""
股票量化研究系统 —— 可直接运行
├── 股票业务 ── K线走势 / 各项指标
├── 模拟持仓 ── 持仓查询 / 买入卖出 / 历史记录 / 交易预测
└── 系统管理 ── 更新数据 / 清空缓存 / 接口版本

依赖: pip install duckdb pyarrow pandas matplotlib
前提: Windows + 通达信客户端已运行 + PYPlugins/user/tqcenter.py 可 import
运行: python stock_app.py --help
"""

import os, sys, glob, time, threading, argparse, platform
from datetime import datetime
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd
import duckdb

from logger import get_logger
import config as _cfg
_log = get_logger("stock_app")


# ====================================================================
# 第 0 层: TDX 行情客户端 (内嵌, 不依赖外部文件)
# ====================================================================
def _init_tdx():
    """定位通达信 PYPlugins/user 并返回 tq 模块

    优先使用环境变量 TDX_INSTALL_DIR (跨平台/免注册表), 否则按注册表卸载键扫描。
    """
    import winreg
    from config import TDX_INSTALL_DIR
    if TDX_INSTALL_DIR:
        sys.path.insert(0, os.path.join(TDX_INSTALL_DIR, "PYPlugins", "user"))
        from tqcenter import tq
        tq.initialize(__file__)
        return tq
    keys = [
        r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\通达信金融终端64",
        r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\通达信专业版",
        r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\通达信金融终端(量化模拟)",
        r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\通达信iTendx研究终端",
        r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\通达信金融终端(测试)",
    ]
    for k in keys:
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, k) as h:
                root = winreg.QueryValueEx(h, "InstallLocation")[0]
            sys.path.insert(0, os.path.join(root, "PYPlugins", "user"))
            from tqcenter import tq
            tq.initialize(__file__)
            return tq
        except FileNotFoundError:
            continue
    raise RuntimeError("未找到通达信安装目录")


# ====================================================================
# 第 1 层: K线存储 (Parquet + DuckDB)
# ====================================================================
class KlineStore:
    """每票一个 Parquet 文件 + DuckDB 元表 + 全量 SQL 查询"""

    def __init__(self, root=None):
        root = root or str(_cfg.DATA_DIR)
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.con = duckdb.connect(str(self.root / "market.duckdb"))
        self._lock = threading.RLock()
        self.con.execute("""
            CREATE TABLE IF NOT EXISTS kline_meta (
                code       VARCHAR,
                period     VARCHAR,
                last_date  DATE,
                row_count  BIGINT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY(code, period))""")

    def _path(self, code, period):
        p = self.root / "kline" / period / f"{code}.parquet"
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    # ---- 写 ----
    def upsert(self, code, df, period="1d", dividend_type="front"):
        """合并写入 K线 (按 date 去重, 新数据覆盖旧数据)

        线程安全 (进程内 RLock) + 原子写 (tmp 文件 + os.replace),
        避免 Flask 多线程并发写同一 parquet 造成损坏。
        """
        if df is None or len(df) == 0:
            return 0
        df = df.copy()
        if isinstance(df.index, pd.DatetimeIndex):
            idx_name = df.index.name
            df = df.reset_index()
            df = df.rename(columns={(idx_name if idx_name else "index"): "date"})
        if "date" not in df.columns:
            # 兜底: 如果列里有任何 datetime 列, 取第一列
            for c in df.columns:
                if pd.api.types.is_datetime64_any_dtype(df[c]):
                    df = df.rename(columns={c: "date"}); break
        df["date"] = pd.to_datetime(df["date"])
        df["code"] = code
        df["period"] = period
        df["dividend_type"] = dividend_type
        wanted = ["date", "code", "period", "dividend_type",
                  "Open", "High", "Low", "Close", "Volume", "Amount"]
        for c in wanted:
            if c not in df.columns:
                df[c] = None
        df = df[wanted]

        path = self._path(code, period)
        with self._lock:
            if path.exists():
                old = pd.read_parquet(path)
                if len(old) and "dividend_type" in old.columns:
                    mixed = set(old["dividend_type"].dropna().unique())
                    if mixed and mixed != {dividend_type}:
                        _log.warning(
                            "K线 %s(%s) 库内复权口径 %s 与写入口径 %s 不一致, "
                            "以新数据覆盖同日期行",
                            code, period, sorted(mixed), dividend_type)
                # 新数据覆盖旧数据: 显式剔除 old 中与新数据重复的日期,
                # 避免 sort_values + drop_duplicates 的非稳定排序保留旧行
                new_dates = set(df["date"].unique())
                old_kept = old[~old["date"].isin(new_dates)]
                df_all = pd.concat([df, old_kept], ignore_index=True)\
                    .sort_values("date").reset_index(drop=True)
            else:
                df_all = df
            tmp = path.with_suffix(".parquet.tmp")
            # row_group_size: 分片写入, 便于 load_tail/get_date_range 只读首尾行组
            df_all.to_parquet(tmp, index=False, compression="zstd",
                              row_group_size=5000)
            os.replace(tmp, path)

            last = df_all["date"].max()
            now = datetime.now()
            self.con.execute(
                "INSERT INTO kline_meta(code, period, last_date, row_count, updated_at) "
                "VALUES(?,?,?,?,?) "
                "ON CONFLICT(code, period) DO UPDATE SET "
                "last_date=excluded.last_date, row_count=excluded.row_count, "
                "updated_at=?",
                [code, period, last, len(df_all), now, now])
            return len(df_all)

    def upsert_many(self, items, period="1d", dividend_type="front", workers=4):
        """批量写多只票 K线 —— 并行写不同 parquet + 批量更新 meta

        items: iterable of (code, df)
        返回 (written, errors)

        与 upsert 语义一致 (新数据覆盖同日期行), 但:
        - 不同 code 写不同文件, 天然无冲突, 可并行 (无需全局锁)
        - kline_meta 用 DataFrame 批量 INSERT..SELECT, 避免单行事务 (52ms/行)
        """
        if not items:
            return 0, 0
        written = errors = 0
        meta_rows = []

        def _write_one(code, df):
            try:
                if df is None or len(df) == 0:
                    return code, None, 0, None
                df2 = df.copy()
                if isinstance(df2.index, pd.DatetimeIndex):
                    df2 = df2.reset_index().rename(columns={"index": "date"})
                if "date" not in df2.columns:
                    for c in df2.columns:
                        if pd.api.types.is_datetime64_any_dtype(df2[c]):
                            df2 = df2.rename(columns={c: "date"}); break
                df2["date"] = pd.to_datetime(df2["date"])
                df2["code"] = code
                df2["period"] = period
                df2["dividend_type"] = dividend_type
                wanted = ["date", "code", "period", "dividend_type",
                          "Open", "High", "Low", "Close", "Volume", "Amount"]
                for c in wanted:
                    if c not in df2.columns:
                        df2[c] = None
                df2 = df2[wanted]

                path = self._path(code, period)
                if path.exists():
                    old = pd.read_parquet(path)
                    new_dates = set(df2["date"].unique())
                    old_kept = old[~old["date"].isin(new_dates)]
                    df_all = pd.concat([df2, old_kept], ignore_index=True)\
                        .sort_values("date").reset_index(drop=True)
                else:
                    df_all = df2
                tmp = path.with_suffix(".parquet.tmp")
                df_all.to_parquet(tmp, index=False, compression="zstd",
                                  row_group_size=5000)
                os.replace(tmp, path)
                return code, df_all["date"].max(), len(df_all), None
            except Exception as e:
                return code, None, 0, e

        now = datetime.now()
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(_write_one, c, d) for c, d in items]
            for fut in as_completed(futures):
                code, last, cnt, err = fut.result()
                if err:
                    errors += 1
                    _log.warning("upsert_many %s fail: %s", code, err)
                else:
                    written += 1
                    meta_rows.append([code, period, last, cnt, now, now])

        if meta_rows:
            mdf = pd.DataFrame(meta_rows, columns=[
                "code", "period", "last_date", "row_count", "updated_at", "upd2"])
            self.con.register("_meta_tmp", mdf)
            try:
                self.con.execute("""
                    INSERT INTO kline_meta(code, period, last_date, row_count, updated_at)
                    SELECT code, period, last_date, row_count, updated_at FROM _meta_tmp
                    ON CONFLICT(code, period) DO UPDATE SET
                        last_date=excluded.last_date, row_count=excluded.row_count,
                        updated_at=excluded.updated_at""")
            finally:
                self.con.unregister("_meta_tmp")
        return written, errors

    # ---- 读单票 ----
    def load(self, code, start=None, end=None, period="1d"):
        path = self._path(code, period)
        if not path.exists():
            raise FileNotFoundError(f"无数据: {code} period={period}")
        df = pd.read_parquet(path)
        if start: df = df[df["date"] >= pd.to_datetime(start)]
        if end:   df = df[df["date"] <= pd.to_datetime(end)]
        return df.sort_values("date").set_index("date")

    def load_tail(self, code, n, period="1d", columns=None):
        """只读最近 n 行 (按行组跳过旧数据, 避免全量 I/O)

        columns: 默认 OHLCV + Amount; 返回 date 索引 DataFrame。
        """
        path = self._path(code, period)
        if not path.exists():
            raise FileNotFoundError(f"无数据: {code} period={period}")
        cols = ["date"] + (columns or ["Open", "High", "Low", "Close",
                                       "Volume", "Amount"])
        import pyarrow.parquet as _pq
        pf = _pq.ParquetFile(path)
        nrg = pf.num_row_groups
        if nrg <= 1:
            df = pf.read(columns=cols).to_pandas()
        else:
            need = n
            rgs = []
            for i in range(nrg - 1, -1, -1):
                rgs.insert(0, i)
                need -= pf.metadata.row_group(i).num_rows
                if need <= 0:
                    break
            df = pf.read_row_groups(rgs, columns=cols).to_pandas()
        return df.sort_values("date").tail(n).set_index("date")

    # ---- DuckDB 全量 SQL ----
    def sql(self, query):
        pattern = str(self.root / "kline" / "*" / "*.parquet").replace("\\", "/")
        if "read_parquet" not in query.lower():
            query = query.replace("FROM kline",
                                  f"FROM read_parquet('{pattern}')")
        return self.con.execute(query).df()

    # ---- 运维 ----
    def list_codes(self, period="1d"):
        return [p.stem for p in (self.root / "kline" / period).glob("*.parquet")]

    def meta(self):
        return self.con.execute(
            "SELECT * FROM kline_meta ORDER BY last_date DESC").df()

    # ---- 检查/范围 ----
    def has_data(self, code, period="1d"):
        return self._path(code, period).exists()

    def get_date_range(self, code, period="1d"):
        p = self._path(code, period)
        if not p.exists():
            return None
        import pyarrow.parquet as _pq
        pf = _pq.ParquetFile(p)
        nrg = pf.num_row_groups
        if nrg <= 2:
            df = pf.read(columns=["date"]).to_pandas()
        else:
            # 仅读首尾行组即可得 min/max (行组按日期有序写入)
            df = pd.concat([
                pf.read_row_groups([0], columns=["date"]).to_pandas(),
                pf.read_row_groups([nrg - 1], columns=["date"]).to_pandas(),
            ], ignore_index=True)
        return {"min": df["date"].min(), "max": df["date"].max(),
                "rows": pf.metadata.num_rows}

    # ---- 删除 ----
    def delete_code(self, code, period="1d"):
        p = self._path(code, period)
        if p.exists():
            p.unlink()
        self.con.execute(
            "DELETE FROM kline_meta WHERE code=? AND period=?", [code, period])

    def delete_all(self):
        import shutil
        kline_dir = self.root / "kline"
        if kline_dir.exists():
            shutil.rmtree(kline_dir)
            kline_dir.mkdir(parents=True, exist_ok=True)
        self.con.execute("DELETE FROM kline_meta")

    def close(self):
        self.con.close()


# ====================================================================
# 第 2 层: 模拟持仓 & 交易记录 (DuckDB)
# ====================================================================
class PortfolioStore:
    """多用户隔离持仓 + 交易流水 + 自选股, 基于 DuckDB"""

    def __init__(self, db_path="data/portfolio.duckdb", init_cash=1_000_000):
        import hashlib, secrets, uuid as _uuid
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.con = duckdb.connect(db_path)
        self.init_cash = float(init_cash)

        self.con.execute(f"""
            CREATE TABLE IF NOT EXISTS users (
                id          INTEGER PRIMARY KEY,
                username    VARCHAR UNIQUE NOT NULL,
                password    VARCHAR NOT NULL,
                salt        VARCHAR DEFAULT '',
                cash        DOUBLE NOT NULL DEFAULT {self.init_cash},
                init_cash   DOUBLE NOT NULL DEFAULT {self.init_cash},
                display_name VARCHAR DEFAULT '',
                created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
        self.con.execute("""
            CREATE TABLE IF NOT EXISTS position (
                user_id    INTEGER NOT NULL,
                code       VARCHAR NOT NULL,
                name       VARCHAR,
                quantity   INTEGER NOT NULL DEFAULT 0,
                cost_price DOUBLE  NOT NULL DEFAULT 0,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (user_id, code))""")
        self.con.execute("""
            CREATE TABLE IF NOT EXISTS trade_log (
                id           INTEGER PRIMARY KEY,
                user_id      INTEGER NOT NULL,
                trade_time   TIMESTAMP,
                code         VARCHAR,
                name         VARCHAR,
                side         VARCHAR,
                price        DOUBLE,
                quantity     INTEGER,
                amount       DOUBLE,
                reason       VARCHAR,
                balance_after DOUBLE)""")
        self.con.execute("""
            CREATE TABLE IF NOT EXISTS watchlist (
                user_id    INTEGER NOT NULL,
                code       VARCHAR NOT NULL,
                name       VARCHAR,
                added_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (user_id, code))""")
        # 旧表迁移: 补 user_id 列
        for tbl in ("position", "trade_log", "watchlist"):
            try:
                cols = [c[0] for c in self.con.execute(f"DESCRIBE {tbl}").fetchall()]
                if 'user_id' not in cols:
                    self.con.execute(f"ALTER TABLE {tbl} ADD COLUMN user_id INTEGER DEFAULT 1")
                    self.con.execute(f"UPDATE {tbl} SET user_id=1 WHERE user_id IS NULL")
            except Exception:
                pass
        # 密码加盐迁移: 补 salt 列
        try:
            cols = [c[0] for c in self.con.execute("DESCRIBE users").fetchall()]
            if 'salt' not in cols:
                self.con.execute("ALTER TABLE users ADD COLUMN salt VARCHAR DEFAULT ''")
        except Exception:
            pass
        self._migrate_legacy()

    @staticmethod
    def _hash_salted(salt, pwd):
        """加盐双重哈希: sha256(salt + sha256(salt + pwd))"""
        import hashlib
        inner = hashlib.sha256((salt + pwd).encode()).hexdigest()
        return hashlib.sha256((salt + inner).encode()).hexdigest()

    def _hash_new(self, pwd):
        import secrets
        salt = secrets.token_hex(16)
        return salt, self._hash_salted(salt, pwd)

    def _migrate_legacy(self):
        """旧版本单用户数据迁移到 user_id=1"""
        has_users = self.con.execute("SELECT COUNT(*) FROM users").fetchone()[0] > 0
        if not has_users:
            self.con.execute("""
                INSERT INTO users(id, username, password, cash, init_cash, display_name)
                VALUES(1, 'default', 'legacy', ?, ?, '默认账户')""",
                [self.init_cash, self.init_cash])

    # ---- 用户认证 ----
    def register(self, username, password, display_name=""):
        username = username.strip()
        if not username or len(username) < 2:
            raise ValueError("用户名至少2位")
        if not password or len(password) < 4:
            raise ValueError("密码至少4位")
        exist = self.con.execute("SELECT id FROM users WHERE username=?", [username]).fetchone()
        if exist:
            raise ValueError("用户名已存在")
        salt, pwd_hash = self._hash_new(password)
        max_id = self.con.execute("SELECT COALESCE(MAX(id), 0) FROM users").fetchone()[0]
        uid = int(max_id) + 1
        self.con.execute(
            "INSERT INTO users(id,username,password,salt,cash,init_cash,display_name) VALUES(?,?,?,?,?,?,?)",
            [uid, username, pwd_hash, salt, self.init_cash, self.init_cash, display_name or username])
        return self._user_info(uid)

    def login(self, username, password):
        row = self.con.execute(
            "SELECT id, username, password, salt FROM users WHERE username=?", [username]).fetchone()
        if not row:
            raise ValueError("用户名或密码错误")
        uid, uname, stored, salt = row[0], row[1], row[2], row[3] or ""

        ok = False
        if salt:
            ok = self._hash_salted(salt, password) == stored
        else:
            # 旧格式: 纯 SHA256 直接比; 明文 legacy 也放行
            import hashlib
            ok = (hashlib.sha256(password.encode()).hexdigest() == stored
                  or stored == password)
            if ok and stored != self._hash_salted(self._hash_new(password)[0], password):
                salt_new, hash_new = self._hash_new(password)
                self.con.execute("UPDATE users SET password=?, salt=? WHERE id=?", [hash_new, salt_new, uid])

        if not ok:
            raise ValueError("用户名或密码错误")
        return self._user_info(uid)

    def get_user(self, uid):
        row = self.con.execute(
            "SELECT id, username, display_name, cash, init_cash, created_at FROM users WHERE id=?", [uid]).fetchone()
        if not row:
            return None
        return self._user_info_from_row(row)

    def list_users(self):
        rows = self.con.execute(
            "SELECT id, username, display_name, cash, init_cash, created_at FROM users ORDER BY id").fetchall()
        return [self._user_info_from_row(r) for r in rows]

    def _user_info(self, uid):
        row = self.con.execute(
            "SELECT id, username, display_name, cash, init_cash, created_at FROM users WHERE id=?", [uid]).fetchone()
        return self._user_info_from_row(row)

    def _user_info_from_row(self, row):
        if not row or len(row) < 2:
            return None
        return {
            "id": row[0],
            "username": row[1],
            "display_name": (row[2] if len(row) > 2 else None) or row[1],
            "cash": float(row[3]) if len(row) > 3 else self.init_cash,
            "init_cash": float(row[4]) if len(row) > 4 else self.init_cash,
            "created_at": str(row[5]) if len(row) > 5 and row[5] else "",
        }

    def _ensure_seq(self, user_id):
        pass

    def _cash(self, user_id):
        row = self.con.execute("SELECT cash FROM users WHERE id=?", [user_id]).fetchone()
        return float(row[0]) if row else self.init_cash

    def _set_cash(self, user_id, cash):
        self.con.execute("UPDATE users SET cash=? WHERE id=?", [float(cash), user_id])

    def _next_seq(self, user_id):
        row = self.con.execute(
            "SELECT COALESCE(MAX(id),0) FROM trade_log").fetchone()
        return (int(row[0]) + 1) if row else 1

    # ---- 买入 ----
    def buy(self, user_id, code, name, price, quantity, reason=""):
        amount = price * quantity
        cash = self._cash(user_id)
        if amount > cash:
            raise ValueError(f"余额不足: 需要 {amount:.2f}, 可用 {cash:.2f}")
        cash -= amount
        self._set_cash(user_id, cash)
        row = self.con.execute(
            "SELECT quantity, cost_price FROM position WHERE user_id=? AND code=?", [user_id, code]).fetchone()
        if row:
            old_qty, old_cost = row
            new_qty = old_qty + quantity
            new_cost = (old_cost * old_qty + price * quantity) / new_qty
            self.con.execute(
                "UPDATE position SET quantity=?, cost_price=?, updated_at=CURRENT_TIMESTAMP WHERE user_id=? AND code=?",
                [new_qty, new_cost, user_id, code])
        else:
            self.con.execute(
                "INSERT INTO position(user_id,code,name,quantity,cost_price) VALUES(?,?,?,?,?)",
                [user_id, code, name, quantity, price])
        self._log(user_id, "BUY", code, name, price, quantity, amount, reason, cash)

    # ---- 卖出 ----
    def sell(self, user_id, code, name, price, quantity, reason=""):
        row = self.con.execute(
            "SELECT quantity FROM position WHERE user_id=? AND code=?", [user_id, code]).fetchone()
        if not row or row[0] < quantity:
            have = row[0] if row else 0
            raise ValueError(f"持仓不足: {code} 持有 {have}, 要卖 {quantity}")
        cash = self._cash(user_id) + price * quantity
        self._set_cash(user_id, cash)
        remain = row[0] - quantity
        if remain == 0:
            self.con.execute("DELETE FROM position WHERE user_id=? AND code=?", [user_id, code])
        else:
            self.con.execute(
                "UPDATE position SET quantity=?, updated_at=CURRENT_TIMESTAMP WHERE user_id=? AND code=?",
                [remain, user_id, code])
        self._log(user_id, "SELL", code, name, price, quantity, price * quantity, reason, cash)

    def _log(self, user_id, side, code, name, price, qty, amount, reason, balance):
        self.con.execute(
            "INSERT INTO trade_log(id,user_id,trade_time,code,name,side,price,quantity,amount,reason,balance_after) "
            "VALUES(?,?,CURRENT_TIMESTAMP,?,?,?,?,?,?,?,?)",
            [self._next_seq(user_id), user_id, code, name, side, price, qty, amount, reason, balance])

    # ---- 查询 ----
    def positions(self, user_id):
        return self.con.execute(
            "SELECT * FROM position WHERE user_id=? ORDER BY updated_at", [user_id]).df()

    def trades(self, user_id, limit=100, offset=0):
        total = self.con.execute(
            "SELECT COUNT(*) FROM trade_log WHERE user_id=?", [user_id]).fetchone()[0]
        df = self.con.execute(
            "SELECT * FROM trade_log WHERE user_id=? ORDER BY trade_time DESC LIMIT ? OFFSET ?",
            [user_id, limit, offset]).df()
        return df, int(total)

    def adjust_cash(self, user_id, delta, reason='人工调整'):
        delta = float(delta)
        if delta == 0:
            return self._cash(user_id)
        cash = self._cash(user_id) + delta
        self._set_cash(user_id, cash)
        self._log(user_id, "ADJUST", "CASH", reason, 0.0, 1, delta, reason, cash)
        return cash

    def set_cash(self, user_id, new_cash, reason='人工设置总资产'):
        delta = float(new_cash) - self._cash(user_id)
        return self.adjust_cash(user_id, delta, reason)

    def cash_balance(self, user_id):
        return self._cash(user_id)

    # ---- 自选列表 ----
    def watchlist(self, user_id):
        return self.con.execute(
            "SELECT code, name, added_at FROM watchlist WHERE user_id=? ORDER BY added_at", [user_id]).df()

    def watchlist_add(self, user_id, code, name=""):
        self.con.execute(
            "INSERT OR IGNORE INTO watchlist(user_id,code,name) VALUES(?,?,?)",
            [user_id, code, name or code])

    def watchlist_remove(self, user_id, code):
        self.con.execute("DELETE FROM watchlist WHERE user_id=? AND code=?", [user_id, code])

    def watchlist_clear(self, user_id):
        self.con.execute("DELETE FROM watchlist WHERE user_id=?", [user_id])

    def close(self):
        self.con.close()


# ====================================================================
# 第 3 层: 核心业务应用
# ====================================================================
class StockApp:
    VERSION = "1.0.0"

    def __init__(self, data_dir=None, init_cash=1_000_000):
        data_dir = data_dir or str(_cfg.DATA_DIR)
        self.tq = _init_tdx()
        self.kline = KlineStore(data_dir)
        self.portfolio = PortfolioStore(
            os.path.join(data_dir, "portfolio.duckdb"), init_cash=init_cash)

    # ----------------------------------------------------------
    # 股票业务
    # ----------------------------------------------------------
    def resolve(self, keyword):
        hits = self.tq.get_match_stkinfo(key_word=keyword) or []
        if not hits and "." in keyword:
            return keyword, keyword
        if not hits:
            raise ValueError(f"未找到: {keyword}")
        for h in hits:
            if h["Code"].lower() == keyword.lower():
                return h["Code"], h["Name"]
        return hits[0]["Code"], hits[0]["Name"]

    def fetch_and_store(self, code, count=24000, period="1d",
                        dividend_type="front"):
        """拉取 K 线并存 Parquet"""
        raw = self.tq.get_market_data(
            field_list=[], stock_list=[code], period=period,
            count=count, dividend_type=dividend_type)
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
            raise ValueError(f"无法获取 {code} 的K线数据")
        df.index = pd.to_datetime(df.index.astype(str))
        n = self.kline.upsert(code, df.reset_index(), period=period,
                              dividend_type=dividend_type)
        return n

    def update(self, codes=None, period="1d", count=24000):
        """更新指定代码 (或已存储的全部代码)"""
        if codes is None:
            codes = self.kline.list_codes(period)
        total, ok, fail = 0, 0, 0
        for code in codes:
            try:
                n = self.fetch_and_store(code, count=count, period=period)
                total += n; ok += 1
            except Exception as e:
                fail += 1
                print(f"  [FAIL] {code}: {e}")
        return total, ok, fail

    def kline_chart(self, code, start=None, end=None, period="1d"):
        """打印 ASCII 简化走势 + 均线信息"""
        df = self.kline.load(code, start, end, period=period)
        ma5  = df["Close"].rolling(5).mean().iloc[-1]
        ma20 = df["Close"].rolling(20).mean().iloc[-1]
        last = df["Close"].iloc[-1]
        prev = df["Close"].iloc[-2] if len(df) >= 2 else last
        ret  = (last / prev - 1) * 100
        print(f"\n{'='*60}")
        print(f" {code}  |  {df.index[0].date()} ~ {df.index[-1].date()}  |  共 {len(df)} 条")
        print(f"{'='*60}")
        print(f"  最新收盘 : {last:.2f}   日涨跌 : {ret:+.2f}%")
        print(f"  MA5={ma5:.2f}   MA20={ma20:.2f}   {'多头排列 ↑' if last>ma5>ma20 else '空头排列 ↓'}")
        print(f"  最高 {df['High'].max():.2f}   最低 {df['Low'].min():.2f}   成交 {df['Amount'].sum():.0f}万")
        print(f"{'─'*60}")
        print(df.tail(10).to_string())
        return df

    def indicators(self, code):
        """专业金融指标: 基础信息 + 扩展信息 + 最新快照"""
        snap = self.tq.get_market_snapshot(stock_code=code)
        more = self.tq.get_more_info(stock_code=code)
        info = self.tq.get_stock_info(stock_code=code, field_list=[
            "Name", "tdx_dyname", "rs_hyname", "J_start",
            "ActiveCapital", "J_zgb"])
        print(f"\n{'='*60}")
        print(f" {info.get('Name','?')} ({code})  —— 专业指标")
        print(f"{'='*60}")
        print(f"  行业     : {info.get('rs_hyname','')}  |  地域 : {info.get('tdx_dyname','')}")
        print(f"  上市日期 : {info.get('J_start','')}")
        print(f"  总股本   : {info.get('J_zgb','')} 万股   流通 : {info.get('ActiveCapital','')} 万股")
        print(f"{'─'*60}")
        print(f"  现价={snap.get('Now')}  涨={more.get('ZAF','')}%  换手={more.get('fHSL','')}%  量比={more.get('fLianB','')}")
        print(f"  振幅={more.get('Zangsu','')}  均价={snap.get('Average','')}")
        print(f"{'─'*60}")
        print(f"  PE(动) ={more.get('DynaPE','')}   PB(MRQ) ={more.get('PB_MRQ','')}")
        print(f"  总市值 : {more.get('Zsz','')} 亿   流通市值 : {more.get('Ltsz','')} 亿")
        print(f"  52周高 ={more.get('HisHigh','')}   52周低 ={more.get('HisLow','')}")
        print(f"  股息率 : {more.get('DYRatio','')}%   Beta ={more.get('BetaValue','')}")
        print(f"{'─'*60}")
        print(f"  主力净额 : {more.get('Zjl','')} 万   连板天 : {more.get('EverZTCount','')}")
        print(f"  封单额   : {more.get('FCAmo','')} 万   涨停价={more.get('ZTPrice','')}  跌停价={more.get('DTPrice','')}")
        return {"snap": snap, "more": more, "info": info}

    # ----------------------------------------------------------
    # 模拟持仓 & 交易
    # ----------------------------------------------------------
    def do_buy(self, keyword, amount_cash=None, price=None, quantity=None, user_id=1):
        """按资金或按数量买入"""
        code, name = self.resolve(keyword)
        if price is None:
            snap = self.tq.get_market_snapshot(stock_code=code)
            price = float(snap.get("Now") or snap.get("LastClose") or 0)
            if price <= 0:
                raise RuntimeError(f"无法获取 {code} 实时价格")
        if quantity is None:
            if amount_cash is None:
                raise ValueError("amount_cash 或 quantity 二选一")
            quantity = int(amount_cash / price / 100) * 100
            if quantity <= 0:
                raise ValueError("资金不够买一手")
        self.portfolio.buy(user_id, code, name, price, quantity)
        print(f"[买入] {name}({code}) 价={price:.2f} 量={quantity} "
              f"金额={price*quantity:,.0f} 元  余额={self.portfolio.cash_balance(user_id):,.0f}")

    def do_sell(self, keyword, price=None, quantity=None, pct=None, user_id=1):
        code, name = self.resolve(keyword)
        if price is None:
            snap = self.tq.get_market_snapshot(stock_code=code)
            price = float(snap.get("Now") or snap.get("LastClose") or 0)
            if price <= 0:
                raise RuntimeError(f"无法获取 {code} 实时价格")
        pos_df = self.portfolio.positions(user_id)
        row = pos_df[pos_df["code"] == code]
        if row.empty:
            raise ValueError(f"无持仓: {code}")
        hold = int(row.iloc[0]["quantity"])
        if quantity is None:
            if pct is not None:
                quantity = int(hold * pct / 100 / 100) * 100
            else:
                quantity = hold
        if quantity <= 0:
            raise ValueError("数量无效")
        self.portfolio.sell(user_id, code, name, price, quantity)
        print(f"[卖出] {name}({code}) 价={price:.2f} 量={quantity} "
              f"金额={price*quantity:,.0f} 元  余额={self.portfolio.cash_balance(user_id):,.0f}")

    def show_positions(self, user_id=1):
        pos = self.portfolio.positions(user_id)
        if pos.empty:
            print("  当前空仓")
            return pos
        rows = []
        for _, r in pos.iterrows():
            snap = self.tq.get_market_snapshot(stock_code=r["code"])
            now  = float(snap.get("Now") or snap.get("LastClose") or r["cost_price"])
            mkt  = now * int(r["quantity"])
            pnl  = mkt - r["cost_price"] * int(r["quantity"])
            rows.append({
                "code": r["code"], "name": r["name"],
                "qty": int(r["quantity"]), "cost": round(r["cost_price"], 2),
                "now": now, "mkt": round(mkt, 2),
                "pnl": round(pnl, 2),
                "pnl_pct": round(pnl / (r["cost_price"] * int(r["quantity"])) * 100, 2),
            })
        df = pd.DataFrame(rows)
        print(f"\n{'='*72}")
        print(f"  当前持仓  |  可用余额: {self.portfolio.cash_balance(user_id):,.0f} 元")
        print(f"{'='*72}")
        print(df.to_string(index=False))
        total = df["mkt"].sum() if not df.empty else 0
        print(f"{'─'*72}")
        print(f"  持仓市值: {total:,.0f} 元   账户总资产: {total + self.portfolio.cash_balance(user_id):,.0f} 元")
        return df

    def show_trades(self, limit=30, user_id=1):
        df = self.portfolio.trades(user_id, limit=limit)
        if df.empty:
            print("  无交易记录")
            return df
        print(f"\n{'='*72}")
        print(f"  最近 {len(df)} 条交易记录")
        print(f"{'='*72}")
        cols = ["trade_time", "code", "name", "side", "price", "quantity",
                "amount", "reason", "balance_after"]
        print(df[cols].to_string(index=False))
        return df

    def predict(self, code, start=None, end=None, period="1d"):
        """简易 MA 金叉/死叉 + MACD 柱状预测"""
        try:
            df = self.kline.load(code, start, end, period=period)
        except FileNotFoundError:
            n = self.fetch_and_store(code, period=period)
            print(f"  首次拉取 {code}: {n} 条"); time.sleep(0.3)
            df = self.kline.load(code, period=period)

        close = df["Close"]
        ma5   = close.rolling(5).mean()
        ma20  = close.rolling(20).mean()

        # EMA12 / EMA26
        ema12 = close.ewm(span=12, adjust=False).mean()
        ema26 = close.ewm(span=26, adjust=False).mean()
        dif   = ema12 - ema26
        dea   = dif.ewm(span=9, adjust=False).mean()
        macd  = (dif - dea) * 2

        # 金叉 / 死叉
        golden = (ma5.shift(1) <= ma20.shift(1)) & (ma5 > ma20)
        death  = (ma5.shift(1) >= ma20.shift(1)) & (ma5 < ma20)
        signal = []
        for i in range(len(df)):
            if golden.iloc[i]: signal.append(("MA金叉", df.index[i]))
            if death.iloc[i]:  signal.append(("MA死叉", df.index[i]))

        last = close.iloc[-1]
        trend = "多头 ↑" if last > ma5.iloc[-1] > ma20.iloc[-1] else \
                "空头 ↓" if last < ma5.iloc[-1] < ma20.iloc[-1] else "震荡 →"
        macd_bar = macd.iloc[-1]

        print(f"\n{'='*60}")
        print(f" 交易预测 —— {code}")
        print(f"{'='*60}")
        print(f"  最新收盘 : {last:.2f}    MA5={ma5.iloc[-1]:.2f}  MA20={ma20.iloc[-1]:.2f}")
        print(f"  MA 形态  : {trend}")
        print(f"  MACD柱   : {macd_bar:+.4f}   {'红柱放大' if macd.iloc[-1] > macd.iloc[-2] else '绿柱收敛'}")
        print(f"{'─'*60}")
        if signal:
            print("  近期 MA 交叉信号:")
            for name, d in signal[-5:]:
                print(f"    {d.date()}  {name}")
        else:
            print("  近期无 MA 交叉信号")

        # 综合建议 (极简)
        score = 0
        score += 2 if last > ma5.iloc[-1] else -2
        score += 2 if ma5.iloc[-1] > ma20.iloc[-1] else -2
        score += 1 if macd_bar > 0 else -1
        conclusion = "偏多 (建议持有或买入)" if score >= 2 else \
                     "偏空 (建议观望或减仓)" if score <= -2 else "中性 (观望为主)"
        print(f"{'─'*60}")
        print(f"  综合得分: {score:+d}  →  {conclusion}")
        return {"score": score, "ma_trend": trend, "macd_bar": macd_bar}

    # ----------------------------------------------------------
    # 系统管理
    # ----------------------------------------------------------
    def version(self):
        print(f"  通达信 TQ-Python 系统  v{self.VERSION}  |  Python {platform.python_version()}  |  {platform.system()}")

    def clear_cache(self, confirm=False):
        if not confirm:
            ask = input("确认清空所有 Parquet + DuckDB 数据? [y/N]: ").strip().lower()
            confirm = (ask == "y")
        if not confirm:
            print("已取消"); return
        import shutil
        root = Path(self.kline.root)
        for p in root.rglob("*.parquet"):
            p.unlink()
        for p in root.glob("*.duckdb"):
            p.unlink()
        print("  已清空", root)

    def close(self):
        try: self.kline.close()
        except: pass
        try: self.portfolio.close()
        except: pass
        try: self.tq.close()
        except: pass


# ====================================================================
# CLI 命令行入口
# ====================================================================
def main():
    p = argparse.ArgumentParser(prog="stock_app",
                                description="通达信股票量化研究系统")
    p.add_argument("--data", default=None, help=f"数据目录 (默认 {_cfg.DATA_DIR})")
    p.add_argument("--cash", type=float, default=1_000_000, help="初始资金 (默认 100 万)")
    sub = p.add_subparsers(dest="cmd")

    # 股票业务
    a1 = sub.add_parser("kline", help="历史K线走势")
    a1.add_argument("keyword"); a1.add_argument("-n","--count",type=int,default=24000)
    a1.add_argument("-p","--period",default="1d")
    a1.add_argument("-s","--start",default=None)
    a1.add_argument("-e","--end",default=None)

    a2 = sub.add_parser("indicators", help="股票各项指标查询")
    a2.add_argument("keyword")

    # 持仓/交易
    b1 = sub.add_parser("buy", help="买入")
    b1.add_argument("keyword")
    g = b1.add_mutually_exclusive_group(required=True)
    g.add_argument("--cash", type=float, help="按资金 (自动整手)")
    g.add_argument("--qty",  type=int,   help="按数量")
    b1.add_argument("--price", type=float, default=None)
    b1.add_argument("--reason", default="")

    b2 = sub.add_parser("sell", help="卖出")
    b2.add_argument("keyword")
    g = b2.add_mutually_exclusive_group()
    g.add_argument("--qty",  type=int,   default=None)
    g.add_argument("--pct", type=int,    default=None, help="百分比: 25/50/100")
    b2.add_argument("--price", type=float, default=None)
    b2.add_argument("--reason", default="")

    sub.add_parser("positions", help="当前持仓")
    sub.add_parser("trades", help="历史交易记录").add_argument("--limit", type=int, default=30)

    # 预测
    c1 = sub.add_parser("predict", help="交易预测 (MA金叉/MACD)")
    c1.add_argument("keyword"); c1.add_argument("-p","--period",default="1d")

    # 系统管理
    d1 = sub.add_parser("update", help="更新数据")
    d1.add_argument("--codes", nargs="*", default=None, help="代码列表, 默认更新已存全部")
    d1.add_argument("-n","--count",type=int,default=24000)
    d1.add_argument("-p","--period",default="1d")

    sub.add_parser("clear", help="清空缓存")
    sub.add_parser("version", help="接口版本")

    args = p.parse_args()

    if not args.cmd:
        p.print_help()
        print()
        print("常用命令示例:")
        print("  python stock_app.py kline 海康威视 -n 60    # 拉K线+存储")
        print("  python stock_app.py indicators 海康威视      # 查PE/PB/市值")
        print("  python stock_app.py buy 海康威视 --cash 50000")
        print("  python stock_app.py positions                # 看持仓")
        print("  python stock_app.py predict 海康威视         # 预测")
        print("  python stock_app.py update                   # 刷新已存全部票")
        sys.exit(0)

    app = StockApp(args.data, init_cash=args.cash)
    try:
        if args.cmd == "kline":
            code, name = app.resolve(args.keyword)
            print(f"\n>>> 拉取 {name}({code}) ...")
            n = app.fetch_and_store(code, count=args.count, period=args.period)
            print(f"    存储 {n} 条")
            app.kline_chart(code, args.start, args.end, args.period)

        elif args.cmd == "indicators":
            code, name = app.resolve(args.keyword)
            print(f"\n>>> 查询 {name}({code}) ...")
            app.indicators(code)

        elif args.cmd == "buy":
            app.do_buy(args.keyword, amount_cash=args.cash,
                       price=args.price, quantity=args.qty)

        elif args.cmd == "sell":
            app.do_sell(args.keyword, price=args.price,
                        quantity=args.qty, pct=args.pct)

        elif args.cmd == "positions":
            app.show_positions()

        elif args.cmd == "trades":
            app.show_trades(args.limit)

        elif args.cmd == "predict":
            code, name = app.resolve(args.keyword)
            print(f"\n>>> 预测 {name}({code}) ...")
            app.predict(code, period=args.period)

        elif args.cmd == "update":
            total, ok, fail = app.update(args.codes, period=args.period, count=args.count)
            print(f"\n>>> 更新完成: 新增/刷新 {total} 行, 成功 {ok}, 失败 {fail}")

        elif args.cmd == "clear":
            app.clear_cache()

        elif args.cmd == "version":
            app.version()
    finally:
        app.close()


if __name__ == "__main__":
    main()
