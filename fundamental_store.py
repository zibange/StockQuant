# Last modified: 2026-08-13 01:10:00
"""
基本面数据存储层 (DuckDB)
=========================
独立数据库 fundamental.duckdb, 采用"长表(EAV) + 元数据 + 更新日志"设计,
便于扩展新指标/新业务, 维护简单。

表结构:
- stock_info       公司基础信息快照 (每票一行, 来自 get_stock_info)
- stock_more       扩展信息快照 (每票一行, 来自 get_more_info)
- financial_facts  专业财务长表 (code, report_date, announce_date, field_code, value)
- gpjy_facts       股票交易专业数据长表 (GP 系列)
- chip_facts       筹码指标长表 (MCST/CYS/ASR/SCR/CYC, 每票每日)
- shareholder_facts 十大股东/十大流通股东明细 (download_file down_type=1)
- field_meta       字段元数据 (字段代码 -> 中文名/类别/来源接口)
- update_log       更新日志 (记录每次拉取范围与结果)

设计要点:
- 长表方案: 任意新增 FN/GP 字段无需改表结构, 天然可扩展
- DuckDB pivot: 需要宽表分析时用 PIVOT 实时转换, 不落冗余宽表
- 单库多域: 财务/交易/筹码后续均可建新表进同库, 统一管理
"""
import os
import sys
import json
from datetime import datetime
from pathlib import Path

import pandas as pd
import duckdb

import config as _cfg
from logger import get_logger
_log = get_logger("fundamental_store")


def _std_out():
    """Windows 控制台 UTF-8 输出兼容"""
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


_std_out()


class FundamentalStore:
    """基本面数据存储: DuckDB 长表方案"""

    def __init__(self, root=None):
        root = root or str(_cfg.DATA_DIR)
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.db_path = self.root / "fundamental.duckdb"
        self.con = duckdb.connect(str(self.db_path))
        self._init_schema()

    # ---------------------------------------------------------------
    # 建表
    # ---------------------------------------------------------------
    def _init_schema(self):
        c = self.con
        # 公司基础信息快照 (每票一行, 最新覆盖)
        c.execute("""
            CREATE TABLE IF NOT EXISTS stock_info (
                code        VARCHAR PRIMARY KEY,
                name        VARCHAR,
                market      VARCHAR,
                industry    VARCHAR,
                region      VARCHAR,
                list_date   VARCHAR,
                total_share DOUBLE,
                float_share DOUBLE,
                is_st       INTEGER,
                is_quit     INTEGER,
                hs_kind     VARCHAR,
                raw_json    VARCHAR,
                updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")

        # 扩展信息快照 (估值/资金/涨跌停等, 每票一行)
        c.execute("""
            CREATE TABLE IF NOT EXISTS stock_more (
                code        VARCHAR PRIMARY KEY,
                hq_date     VARCHAR,
                zaf         DOUBLE,     -- 当日涨幅%
                pe_dyna     DOUBLE,     -- 动态市盈率
                pe_ttm      DOUBLE,     -- 市盈率TTM
                pb_mrq      DOUBLE,     -- 市净率
                dy_ratio    DOUBLE,     -- 股息率%
                total_mv    DOUBLE,     -- 总市值(亿)
                float_mv    DOUBLE,     -- 流通市值(亿)
                hsl         DOUBLE,     -- 换手率%
                lb          DOUBLE,     -- 量比
                beta        DOUBLE,     -- 贝塔
                his_high    DOUBLE,     -- 52周最高
                his_low     DOUBLE,     -- 52周最低
                zt_price    DOUBLE,     -- 涨停价
                dt_price    DOUBLE,     -- 跌停价
                zjl         DOUBLE,     -- 主力净流入(万)
                raw_json    VARCHAR,
                updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")

        # 专业财务长表 (FN 系列)
        c.execute("""
            CREATE TABLE IF NOT EXISTS financial_facts (
                code          VARCHAR NOT NULL,
                report_date   VARCHAR,   -- 报告期 如 20251231
                announce_date VARCHAR,   -- 公告日期
                field_code    VARCHAR NOT NULL,   -- FN1..FN584
                value         DOUBLE,
                updated_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY(code, report_date, field_code))""")

        # 股票交易专业数据长表 (GP 系列)
        c.execute("""
            CREATE TABLE IF NOT EXISTS gpjy_facts (
                code          VARCHAR NOT NULL,
                trade_date    VARCHAR,
                field_code    VARCHAR NOT NULL,   -- GP1..GP46
                value         DOUBLE,
                updated_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY(code, trade_date, field_code))""")

        # 筹码指标长表 (MCST/CYS/ASR/SCR/CYC 等, 每票每日)
        c.execute("""
            CREATE TABLE IF NOT EXISTS chip_facts (
                code          VARCHAR NOT NULL,
                trade_date    VARCHAR,
                field_code    VARCHAR NOT NULL,   -- MCST/CYS/ASR/SCR/CYC1..
                value         DOUBLE,
                updated_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY(code, trade_date, field_code))""")

        # L2 扩展日线长表 (get_exday_data: 委买委卖/大单分布等)
        c.execute("""
            CREATE TABLE IF NOT EXISTS l2_facts (
                code          VARCHAR NOT NULL,
                trade_date    VARCHAR,
                field_code    VARCHAR NOT NULL,   -- CJBS/BOrder/BCancel/BuyAvp/Vol_1_1...
                value         DOUBLE,
                updated_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY(code, trade_date, field_code))""")

        # 十大股东/十大流通股东明细 (download_file down_type=1, 每报告期)
        # holder_type: gd=十大股东 / ltgd=十大流通股东
        c.execute("""
            CREATE TABLE IF NOT EXISTS shareholder_facts (
                code          VARCHAR NOT NULL,
                report_date   VARCHAR,   -- 报告期 如 20241231
                holder_type   VARCHAR,   -- gd / ltgd
                rank          INTEGER,   -- 排名 1-10
                holder_name   VARCHAR,   -- 股东名称
                shares        DOUBLE,    -- 持股数量(股)
                pct           DOUBLE,    -- 持股比例(%)
                updated_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY(code, report_date, holder_type, rank))""")

        # 主营构成明细 (download_file down_type=5, 每报告期)
        # dim_type: 按产品(项目) / 按地区
        c.execute("""
            CREATE TABLE IF NOT EXISTS mainbusi_facts (
                code          VARCHAR NOT NULL,
                report_date   VARCHAR,   -- 报告期 如 20260331
                dim_type      VARCHAR,   -- 按产品(项目) / 按地区
                item_name     VARCHAR,   -- 主营构成名称
                revenue       DOUBLE,    -- 主营收入(元)
                revenue_pct   DOUBLE,    -- 收入比例%
                cost          DOUBLE,    -- 主营成本
                cost_pct      DOUBLE,    -- 成本比例%
                profit        DOUBLE,    -- 毛利
                profit_pct    DOUBLE,    -- 利润比例%
                profit_rate   DOUBLE,    -- 毛利率%
                updated_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY(code, report_date, dim_type, item_name))""")

        # 主营构成概述 (产品名称 + 主营构成文本, 每票一行最新覆盖)
        c.execute("""
            CREATE TABLE IF NOT EXISTS mainbusi_profile (
                code          VARCHAR PRIMARY KEY,
                product_name  VARCHAR,   -- 产品名称
                business_desc VARCHAR,   -- 主营构成描述
                raw_json      VARCHAR,
                updated_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")

        # 字段元数据
        c.execute("""
            CREATE TABLE IF NOT EXISTS field_meta (
                field_code  VARCHAR PRIMARY KEY,
                field_name  VARCHAR,
                category    VARCHAR,   -- financial / gpjy / stock_more / ...
                source_api  VARCHAR)""")

        # 更新日志 (id 用自增序列模拟)
        c.execute("""
            CREATE SEQUENCE IF NOT EXISTS update_log_seq START 1""")
        c.execute("""
            CREATE TABLE IF NOT EXISTS update_log (
                id          INTEGER PRIMARY KEY,
                biz         VARCHAR,   -- stock_info / financial / gpjy / ...
                scope       VARCHAR,   -- 股票代码或范围描述
                detail      VARCHAR,   -- 拉取参数/条数
                status      VARCHAR,   -- ok / error
                msg         VARCHAR,
                started_at  TIMESTAMP,
                finished_at TIMESTAMP)""")

    # ---------------------------------------------------------------
    # 字段元数据
    # ---------------------------------------------------------------
    def register_fields(self, field_map, category, source_api):
        """批量登记字段元数据; field_map: {code: 中文名}"""
        rows = [(k, v, category, source_api) for k, v in field_map.items()]
        self.con.executemany(
            "INSERT INTO field_meta(field_code, field_name, category, source_api) "
            "VALUES(?,?,?,?) "
            "ON CONFLICT(field_code) DO UPDATE SET "
            "field_name=excluded.field_name, category=excluded.category, "
            "source_api=excluded.source_api",
            rows)

    # ---------------------------------------------------------------
    # stock_info (快照, 覆盖式)
    # ---------------------------------------------------------------
    def upsert_stock_info(self, info: dict):
        """info 为 get_stock_info 返回的原始字典(已扁平)"""
        if not info:
            return
        code = info.get("Code") or info.get("code")
        if not code:
            return
        now = datetime.now()
        self.con.execute("""
            INSERT INTO stock_info(code, name, market, industry, region,
                                   list_date, total_share, float_share,
                                   is_st, is_quit, hs_kind, raw_json, updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(code) DO UPDATE SET
                name=excluded.name, market=excluded.market,
                industry=excluded.industry, region=excluded.region,
                list_date=excluded.list_date,
                total_share=excluded.total_share, float_share=excluded.float_share,
                is_st=excluded.is_st, is_quit=excluded.is_quit,
                hs_kind=excluded.hs_kind, raw_json=excluded.raw_json,
                updated_at=excluded.updated_at""",
            [code,
             info.get("Name"), info.get("market"),
             info.get("rs_hyname") or info.get("industry"),
             info.get("tdx_dyname") or info.get("region"),
             info.get("J_start") or info.get("list_date"),
             self._to_float(info.get("J_zgb")), self._to_float(info.get("ActiveCapital")),
             1 if str(info.get("IsSTGP")) == "1" else 0,
             1 if str(info.get("IsQuitGP")) == "1" else 0,
             str(info.get("HSStockKind") or ""),
             json.dumps(info, ensure_ascii=False)[:20000], now])

    # ---------------------------------------------------------------
    # stock_more (快照, 覆盖式)
    # ---------------------------------------------------------------
    def upsert_stock_more(self, more: dict):
        if not more:
            return
        code = more.get("Code") or more.get("code")
        if not code:
            return
        now = datetime.now()
        self.con.execute("""
            INSERT INTO stock_more(code, hq_date, zaf, pe_dyna, pe_ttm, pb_mrq,
                                   dy_ratio, total_mv, float_mv, hsl, lb, beta,
                                   his_high, his_low, zt_price, dt_price, zjl,
                                   raw_json, updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(code) DO UPDATE SET
                hq_date=excluded.hq_date, zaf=excluded.zaf,
                pe_dyna=excluded.pe_dyna, pe_ttm=excluded.pe_ttm,
                pb_mrq=excluded.pb_mrq, dy_ratio=excluded.dy_ratio,
                total_mv=excluded.total_mv, float_mv=excluded.float_mv,
                hsl=excluded.hsl, lb=excluded.lb, beta=excluded.beta,
                his_high=excluded.his_high, his_low=excluded.his_low,
                zt_price=excluded.zt_price, dt_price=excluded.dt_price,
                zjl=excluded.zjl, raw_json=excluded.raw_json,
                updated_at=excluded.updated_at""",
            [code, more.get("HqDate"),
             self._to_float(more.get("ZAF")), self._to_float(more.get("DynaPE")),
             self._to_float(more.get("StaticPE_TTM")), self._to_float(more.get("PB_MRQ")),
             self._to_float(more.get("DYRatio")), self._to_float(more.get("Zsz")),
             self._to_float(more.get("Ltsz")), self._to_float(more.get("fHSL")),
             self._to_float(more.get("fLianB")), self._to_float(more.get("BetaValue")),
             self._to_float(more.get("HisHigh")), self._to_float(more.get("HisLow")),
             self._to_float(more.get("ZTPrice")), self._to_float(more.get("DTPrice")),
             self._to_float(more.get("Zjl")),
             json.dumps(more, ensure_ascii=False)[:20000], now])

    def upsert_stock_mores(self, mores: list):
        """批量 upsert stock_more (向量化, 单条 INSERT..SELECT 替代逐行循环)

        DuckDB 单行 INSERT 在 autocommit 下每行一个事务, 5553 行会极慢;
        本方法用 DataFrame 注册 + INSERT..SELECT ON CONFLICT 一次写入全批。
        """
        if not mores:
            return 0
        rows = []
        now = datetime.now()
        for more in mores:
            code = more.get("Code") or more.get("code")
            if not code:
                continue
            rows.append([
                code, more.get("HqDate"),
                self._to_float(more.get("ZAF")), self._to_float(more.get("DynaPE")),
                self._to_float(more.get("StaticPE_TTM")), self._to_float(more.get("PB_MRQ")),
                self._to_float(more.get("DYRatio")), self._to_float(more.get("Zsz")),
                self._to_float(more.get("Ltsz")), self._to_float(more.get("fHSL")),
                self._to_float(more.get("fLianB")), self._to_float(more.get("BetaValue")),
                self._to_float(more.get("HisHigh")), self._to_float(more.get("HisLow")),
                self._to_float(more.get("ZTPrice")), self._to_float(more.get("DTPrice")),
                self._to_float(more.get("Zjl")),
                json.dumps(more, ensure_ascii=False)[:20000], now,
            ])
        if not rows:
            return 0
        df = pd.DataFrame(rows, columns=[
            "code", "hq_date", "zaf", "pe_dyna", "pe_ttm", "pb_mrq",
            "dy_ratio", "total_mv", "float_mv", "hsl", "lb", "beta",
            "his_high", "his_low", "zt_price", "dt_price", "zjl",
            "raw_json", "updated_at",
        ])
        self.con.register("_mv_tmp", df)
        try:
            self.con.execute("""
                INSERT INTO stock_more(code, hq_date, zaf, pe_dyna, pe_ttm, pb_mrq,
                                       dy_ratio, total_mv, float_mv, hsl, lb, beta,
                                       his_high, his_low, zt_price, dt_price, zjl,
                                       raw_json, updated_at)
                SELECT code, hq_date, zaf, pe_dyna, pe_ttm, pb_mrq,
                       dy_ratio, total_mv, float_mv, hsl, lb, beta,
                       his_high, his_low, zt_price, dt_price, zjl,
                       raw_json, updated_at FROM _mv_tmp
                ON CONFLICT(code) DO UPDATE SET
                    hq_date=excluded.hq_date, zaf=excluded.zaf,
                    pe_dyna=excluded.pe_dyna, pe_ttm=excluded.pe_ttm,
                    pb_mrq=excluded.pb_mrq, dy_ratio=excluded.dy_ratio,
                    total_mv=excluded.total_mv, float_mv=excluded.float_mv,
                    hsl=excluded.hsl, lb=excluded.lb, beta=excluded.beta,
                    his_high=excluded.his_high, his_low=excluded.his_low,
                    zt_price=excluded.zt_price, dt_price=excluded.dt_price,
                    zjl=excluded.zjl, raw_json=excluded.raw_json,
                    updated_at=excluded.updated_at
            """)
        finally:
            self.con.unregister("_mv_tmp")
        return len(rows)

    # ---------------------------------------------------------------
    # financial_facts (长表)
    # ---------------------------------------------------------------
    def upsert_financial(self, code, df):
        """df: pandas DataFrame, 列含 report_date / announce_date / FN* 字段

        长表化: 每行(报告期, 字段)一条记录, 主键冲突时覆盖。
        向量化: melt 展平 + 单条 INSERT..SELECT (替代逐行 Python 循环)。
        """
        if df is None or df.empty:
            return 0
        id_vars = [c for c in ("report_date", "announce_date") if c in df.columns]
        melt = df.melt(id_vars=id_vars, var_name="field_code", value_name="value")
        if "report_date" in melt.columns:
            melt = melt[melt["report_date"].notna()]
        melt["value"] = pd.to_numeric(melt["value"], errors="coerce")
        melt = melt.dropna(subset=["value"])
        if melt.empty:
            return 0
        melt.insert(0, "code", code)
        if "announce_date" not in melt.columns:
            melt["announce_date"] = ""
        melt["report_date"] = melt["report_date"].astype(str)
        melt["announce_date"] = melt["announce_date"].fillna("").astype(str)
        melt["updated_at"] = datetime.now()
        self.con.register("_up_fin", melt[["code", "report_date", "announce_date",
                                           "field_code", "value", "updated_at"]])
        try:
            self.con.execute("""
                INSERT INTO financial_facts(code, report_date, announce_date,
                                            field_code, value, updated_at)
                SELECT code, report_date, announce_date, field_code, value, updated_at
                FROM _up_fin
                ON CONFLICT(code, report_date, field_code) DO UPDATE SET
                    announce_date=excluded.announce_date, value=excluded.value,
                    updated_at=excluded.updated_at""")
        finally:
            self.con.unregister("_up_fin")
        return len(melt)

    # ---------------------------------------------------------------
    # gpjy_facts (长表)
    # ---------------------------------------------------------------
    def upsert_gpjy(self, code, df):
        """df: 列含 trade_date / GP* 字段 (向量化 melt + INSERT..SELECT)"""
        if df is None or df.empty:
            return 0
        melt = df.melt(id_vars=["trade_date"], var_name="field_code",
                       value_name="value")
        melt = melt[melt["trade_date"].notna()]
        melt["value"] = pd.to_numeric(melt["value"], errors="coerce")
        melt = melt.dropna(subset=["value"])
        if melt.empty:
            return 0
        melt.insert(0, "code", code)
        melt["trade_date"] = melt["trade_date"].astype(str)
        melt["updated_at"] = datetime.now()
        self.con.register("_up_gpjy", melt[["code", "trade_date",
                                            "field_code", "value", "updated_at"]])
        try:
            self.con.execute("""
                INSERT INTO gpjy_facts(code, trade_date, field_code, value, updated_at)
                SELECT code, trade_date, field_code, value, updated_at FROM _up_gpjy
                ON CONFLICT(code, trade_date, field_code) DO UPDATE SET
                    value=excluded.value, updated_at=excluded.updated_at""")
        finally:
            self.con.unregister("_up_gpjy")
        return len(melt)

    # ---------------------------------------------------------------
    # chip_facts / l2_facts (通用长表, 表名参数化)
    # ---------------------------------------------------------------
    def upsert_metric_facts(self, table, code, df, date_col="trade_date"):
        """通用指标长表写入 (chip_facts/l2_facts 复用)

        df: DataFrame, 列含 date_col 时间列 + 若干指标列
        """
        assert table in ("chip_facts", "l2_facts"), f"unknown table {table}"
        if df is None or df.empty:
            return 0
        records = []
        for _, row in df.iterrows():
            d = str(row.get(date_col) or "")
            if not d:
                continue
            for col in df.columns:
                col_s = str(col)
                if col_s == date_col:
                    continue
                val = row[col]
                if val is None or pd.isna(val):
                    continue
                try:
                    fval = float(val)
                except (TypeError, ValueError):
                    continue
                records.append((code, d, col_s, fval))
        if not records:
            return 0
        now = datetime.now()
        rows = [tuple(r) + (now,) for r in records]
        self.con.executemany(f"""
            INSERT INTO {table}(code, trade_date, field_code, value, updated_at)
            VALUES(?,?,?,?,?)
            ON CONFLICT(code, trade_date, field_code) DO UPDATE SET
                value=excluded.value, updated_at=excluded.updated_at""", rows)
        return len(records)

    def get_metric_wide(self, table, code, fields=None, limit=300):
        """指标长表 -> 宽表 (trade_date x 指标列), 按日期倒序"""
        assert table in ("chip_facts", "l2_facts", "gpjy_facts"), f"unknown {table}"
        sql = f"SELECT * FROM {table} WHERE code=?"
        args = [code]
        if fields:
            fl = [f.strip() for f in fields.split(",") if f.strip()]
            if fl:
                sql += " AND field_code IN (" + ",".join("?" * len(fl)) + ")"
                args += fl
        df = self.con.execute(sql, args).df()
        if df.empty:
            return pd.DataFrame()
        wide = df.pivot_table(index="trade_date", columns="field_code",
                              values="value", aggfunc="first").reset_index()
        wide.columns = [str(c) for c in wide.columns]
        return wide.sort_values("trade_date", ascending=False).head(limit)

    def has_metric(self, table, code):
        r = self.con.execute(
            f"SELECT COUNT(*) n FROM {table} WHERE code=?", [code]).fetchone()
        return (r[0] if r else 0) > 0

    def metric_date_range(self, table, code):
        r = self.con.execute(
            f"SELECT MIN(trade_date), MAX(trade_date), COUNT(DISTINCT trade_date) "
            f"FROM {table} WHERE code=?", [code]).fetchone()
        return {"min": r[0], "max": r[1], "days": r[2]} if r else None

    # ---------------------------------------------------------------
    # shareholder_facts (十大股东/十大流通股东, 明细覆盖式)
    # ---------------------------------------------------------------
    def upsert_shareholder(self, code, df):
        """df 列为: report_date, holder_type, rank, holder_name, shares, pct"""
        if df is None or df.empty:
            return 0
        records = []
        for _, row in df.iterrows():
            rdate = str(row.get("report_date") or "")
            htype = str(row.get("holder_type") or "")
            rname = str(row.get("holder_name") or "")
            if not rdate or not htype or not rname:
                continue
            try:
                rank = int(row.get("rank"))
            except (TypeError, ValueError):
                continue
            records.append((code, rdate, htype, rank, rname,
                            self._to_float(row.get("shares")),
                            self._to_float(row.get("pct"))))
        if not records:
            return 0
        now = datetime.now()
        rows = [tuple(r) + (now,) for r in records]
        self.con.executemany("""
            INSERT INTO shareholder_facts(code, report_date, holder_type, rank,
                                          holder_name, shares, pct, updated_at)
            VALUES(?,?,?,?,?,?,?,?)
            ON CONFLICT(code, report_date, holder_type, rank) DO UPDATE SET
                holder_name=excluded.holder_name, shares=excluded.shares,
                pct=excluded.pct, updated_at=excluded.updated_at""", rows)
        return len(records)

    def get_shareholder(self, code, holder_type=None, report_date=None):
        """股东明细查询, 返回列: report_date, holder_type, rank, holder_name, shares, pct"""
        sql = "SELECT * FROM shareholder_facts WHERE code=?"
        args = [code]
        if holder_type:
            sql += " AND holder_type=?"
            args.append(holder_type)
        if report_date:
            sql += " AND report_date=?"
            args.append(report_date)
        df = self.con.execute(sql, args).df()
        if df.empty:
            return df
        return df.sort_values(["report_date", "holder_type", "rank"])

    def shareholder_dates(self, code):
        r = self.con.execute(
            "SELECT DISTINCT report_date FROM shareholder_facts WHERE code=? "
            "ORDER BY report_date DESC", [code]).df()
        return [str(x) for x in r["report_date"]] if not r.empty else []

    def has_shareholder(self, code):
        r = self.con.execute(
            "SELECT COUNT(*) n FROM shareholder_facts WHERE code=?",
            [code]).fetchone()
        return (r[0] if r else 0) > 0

    # ---------------------------------------------------------------
    # mainbusi (主营构成, download_file down_type=5)
    # ---------------------------------------------------------------
    def upsert_mainbusi(self, code, df):
        """主营构成明细入库; df 列为: report_date, dim_type, item_name,
        revenue, revenue_pct, cost, cost_pct, profit, profit_pct, profit_rate"""
        if df is None or df.empty:
            return 0
        records = []
        for _, row in df.iterrows():
            rdate = str(row.get("report_date") or "")
            dim = str(row.get("dim_type") or "")
            name = str(row.get("item_name") or "")
            if not rdate or not dim or not name:
                continue
            records.append((code, rdate, dim, name,
                            self._to_float(row.get("revenue")),
                            self._to_float(row.get("revenue_pct")),
                            self._to_float(row.get("cost")),
                            self._to_float(row.get("cost_pct")),
                            self._to_float(row.get("profit")),
                            self._to_float(row.get("profit_pct")),
                            self._to_float(row.get("profit_rate"))))
        if not records:
            return 0
        now = datetime.now()
        rows = [tuple(r) + (now,) for r in records]
        self.con.executemany("""
            INSERT INTO mainbusi_facts(code, report_date, dim_type, item_name,
                                       revenue, revenue_pct, cost, cost_pct,
                                       profit, profit_pct, profit_rate,
                                       updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(code, report_date, dim_type, item_name) DO UPDATE SET
                revenue=excluded.revenue, revenue_pct=excluded.revenue_pct,
                cost=excluded.cost, cost_pct=excluded.cost_pct,
                profit=excluded.profit, profit_pct=excluded.profit_pct,
                profit_rate=excluded.profit_rate,
                updated_at=excluded.updated_at""", rows)
        return len(records)

    def upsert_mainbusi_profile(self, code, product_name, business_desc,
                                raw_json=None):
        if not code:
            return
        now = datetime.now()
        self.con.execute("""
            INSERT INTO mainbusi_profile(code, product_name, business_desc,
                                         raw_json, updated_at)
            VALUES(?,?,?,?,?)
            ON CONFLICT(code) DO UPDATE SET
                product_name=excluded.product_name,
                business_desc=excluded.business_desc,
                raw_json=excluded.raw_json,
                updated_at=excluded.updated_at""",
            [code, product_name, business_desc, raw_json, now])

    def get_mainbusi_profile(self, code):
        """主营构成概述查询 (独立只读连接, 避免与后台同步写连接冲突)"""
    def get_mainbusi_profile(self, code):
        """主营构成概述查询 (独立只读连接, 避免与后台同步写连接冲突)"""
        r = self.con.execute(
            "SELECT product_name, business_desc FROM mainbusi_profile "
            "WHERE code=?", [code]).fetchone()
        return ({"product_name": r[0], "business_desc": r[1]}
                if r else None)

    def get_mainbusi(self, code, report_date=None):
        """主营构成明细查询: code 必填, report_date 可选
        返回列: report_date, dim_type, item_name, revenue, revenue_pct,
                cost, cost_pct, profit, profit_pct, profit_rate
        独立只读连接, 避免与后台同步写连接冲突 (同 list_synced)。
        """
    def get_mainbusi(self, code, report_date=None):
        """主营构成明细查询: code 必填, report_date 可选
        返回列: report_date, dim_type, item_name, revenue, revenue_pct,
                cost, cost_pct, profit, profit_pct, profit_rate
        独立只读连接, 避免与后台同步写连接冲突 (同 list_synced)。
        """
        sql = "SELECT * FROM mainbusi_facts WHERE code=?"
        args = [code]
        if report_date:
            sql += " AND report_date=?"
            args.append(report_date)
        df = self.con.execute(sql, args).df()
        if df.empty:
            return df
        return df.sort_values(["report_date", "dim_type"])

    def mainbusi_dates(self, code):
        r = self.con.execute(
            "SELECT DISTINCT report_date FROM mainbusi_facts WHERE code=? "
            "ORDER BY report_date DESC", [code]).df()
        return [str(x) for x in r["report_date"]] if not r.empty else []

    def has_mainbusi(self, code):
        r = self.con.execute(
            "SELECT COUNT(*) n FROM mainbusi_facts WHERE code=?",
            [code]).fetchone()
        return (r[0] if r else 0) > 0

    # ---------------------------------------------------------------
    # 查询
    # ---------------------------------------------------------------
    def get_stock_info(self, code=None):
        if code:
            return self.con.execute(
                "SELECT * FROM stock_info WHERE code=?", [code]).df()
        return self.con.execute("SELECT * FROM stock_info").df()

    def get_stock_more(self, code=None):
        if code:
            return self.con.execute(
                "SELECT * FROM stock_more WHERE code=?", [code]).df()
        return self.con.execute("SELECT * FROM stock_more").df()

    def get_financial_wide(self, code, field_codes=None, report_date=None):
        """财务长表 -> 宽表 (每行一个报告期)

        field_codes: 指定字段; 默认取全部已存字段
        report_date: 指定报告期
        """
        sql = "SELECT * FROM financial_facts WHERE code=?"
        args = [code]
        if report_date:
            sql += " AND report_date=?"
            args.append(report_date)
        df = self.con.execute(sql, args).df()
        if df.empty:
            return df
        required = {"report_date", "field_code", "value"}
        if not required.issubset(df.columns):
            return df.iloc[0:0]
        if field_codes:
            df = df[df["field_code"].isin(field_codes)]
        if df.empty:
            return df
        wide = df.pivot_table(
            index=["report_date", "announce_date"], columns="field_code",
            values="value", aggfunc="first").reset_index()
        wide.columns = [str(c) for c in wide.columns]
        return wide.sort_values("report_date")

    def get_financial_long(self, code, report_date=None):
        df = self.con.execute(
            "SELECT * FROM financial_facts WHERE code=? AND "
            "(? IS NULL OR report_date=?)",
            [code, report_date, report_date]).df()
        if df.empty:
            return df
        meta = self.con.execute(
            "SELECT field_code, field_name FROM field_meta").df()
        if not meta.empty:
            df = df.merge(meta, on="field_code", how="left")
        return df.sort_values(["report_date", "field_code"])

    def list_financial_dates(self, code):
        return self.con.execute(
            "SELECT DISTINCT report_date FROM financial_facts "
            "WHERE code=? ORDER BY report_date DESC", [code]).df()

    def has_financial(self, code):
        r = self.con.execute(
            "SELECT COUNT(*) n FROM financial_facts WHERE code=?", [code]).fetchone()
        return (r[0] if r else 0) > 0

    # ---------------------------------------------------------------
    # 更新日志
    # ---------------------------------------------------------------
    def log_update(self, biz, scope, detail, status, msg, started, finished):
        self.con.execute("""
            INSERT INTO update_log(id, biz, scope, detail, status, msg,
                                   started_at, finished_at)
            VALUES(nextval('update_log_seq'), ?,?,?,?,?,?,?)""",
            [biz, scope, detail, status, msg, started, finished])

    def recent_logs(self, limit=20):
        return self.con.execute(
            "SELECT * FROM update_log ORDER BY id DESC LIMIT ?",
            [limit]).df()

    # ---------------------------------------------------------------
    # 工具
    # ---------------------------------------------------------------
    @staticmethod
    def _to_float(v):
        try:
            if v is None or v == "" or v == "-":
                return None
            f = float(v)
            if f != f:   # NaN (pandas 缺失值) 显式转 None, 不依赖 DuckDB 隐式转换
                return None
            return f
        except (TypeError, ValueError):
            return None

    def table_summary(self):
        """库内各表行数与存储概览 (供管理端展示)"""
        out = {}
        for t in ["stock_info", "stock_more", "financial_facts",
                  "gpjy_facts", "chip_facts", "l2_facts",
                  "shareholder_facts", "mainbusi_facts", "mainbusi_profile",
                  "field_meta", "update_log"]:
            try:
                n = self.con.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
            except Exception:
                n = 0
            out[t] = n
        size_kb = 0
        if self.db_path.exists():
            size_kb = self.db_path.stat().st_size / 1024
        out["_size_kb"] = round(size_kb, 1)
        return out

    # ---------------------------------------------------------------
    # 已同步股票清单 (轻量分页, 供左侧栏)
    # ---------------------------------------------------------------
    SYNC_BIZ_TABLES = {
        "basic": ("stock_info", "stock_more"),
        "financial": ("financial_facts",),
        "gpjy": ("gpjy_facts",),
        "chip": ("chip_facts",),
        "l2": ("l2_facts",),
        "shareholder": ("shareholder_facts",),
        "mainbusi": ("mainbusi_facts",),
    }


    def list_synced(self, page=1, page_size=10):
        """分页返回已同步股票 + 每票六大类型同步状态 (单条 GROUP BY, 轻量)

        六大类型: basic 基础信息 / financial 专业财务 / gpjy 交易专业 /
                  chip 筹码指标 / l2 L2扩展 / shareholder 股东明细
        使用 self.con (DuckDB MVCC 保证读写不阻塞)。
        返回: {total, page, page_size, items:[{code, name, types:[..]}]}
        """
        page = max(1, int(page))
        page_size = min(50, max(1, int(page_size)))
        parts = []
        for biz, tbls in self.SYNC_BIZ_TABLES.items():
            for t in tbls:
                parts.append(
                    f'SELECT code, \'{biz}\' AS biz FROM "{t}"')
        if not parts:
            return {"total": 0, "page": page, "page_size": page_size,
                    "items": []}
        sel_flags = ", ".join(
            f"COALESCE(MAX(CASE WHEN biz='{b}' THEN 1 ELSE 0 END), 0) AS f_{b}"
            for b in self.SYNC_BIZ_TABLES)
        df = self.con.execute(f"""
            WITH u AS ({' UNION ALL '.join(parts)})
            SELECT code, {sel_flags}
            FROM u GROUP BY code ORDER BY code""").df()
        name_map = {}
        try:
            ndf = self.con.execute(
                "SELECT code, name FROM stock_info").df()
            name_map = dict(zip(ndf["code"], ndf["name"]))
        except Exception:
            pass
        if df.empty:
            return {"total": 0, "page": page, "page_size": page_size,
                    "items": []}
        items = []
        for _, r in df.iterrows():
            types = [b for b in self.SYNC_BIZ_TABLES if r.get(f"f_{b}")]
            items.append({"code": r["code"], "name": name_map.get(r["code"], ""),
                          "types": types})
        total = len(items)
        start = (page - 1) * page_size
        return {"total": total, "page": page, "page_size": page_size,
                "items": items[start:start + page_size]}

    def close(self):
        try:
            self.con.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


if __name__ == "__main__":
    # 自检
    from fundamental_fields import FN_NAME
    with FundamentalStore() as st:
        st.register_fields(FN_NAME, "financial", "get_financial_data")
        print("字段元数据已登记:", st.con.execute(
            "SELECT COUNT(*) FROM field_meta").fetchone()[0])
        print("库概览:", st.table_summary())
