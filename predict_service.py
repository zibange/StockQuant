# Last modified: 2026-08-14 00:50:00
"""
PredictService — 行情预测 + 交易目标售价计算
职责: 封装技术指标评分 + 加仓/不加仓预测公式
"""
from logger import get_logger
_log = get_logger("predict_service")


class PredictService:
    """行情预测 (技术指标评分 + 交易目标售价计算)

    依赖注入:
      kl_store   — KlineStore, 负责本地/远程 K 线数据获取
      tq_client  — TQLocalClient, 通达信行情接口
      portfolio  — PortfolioStore, 模拟持仓查询 (predict_sell 需要)
    """

    def __init__(self, kl_store=None, tq_client=None, portfolio=None):
        self.kl_store = kl_store
        self.tq = tq_client
        self.portfolio = portfolio

    # ================================================================
    # 行情预测: MA + MACD 评分
    # ================================================================
    def predict(self, code):
        """对单票做技术指标评分 (MA5/MA20 + MACD)

        返回 dict:
          code, name, last_price, ma5, ma20, dif, dea, macd_bar,
          ma_trend, score, conclusion, recent_crosses
        """
        if self.kl_store is None or self.tq is None:
            raise RuntimeError("PredictService 缺少依赖: kl_store / tq_client 未注入")

        try:
            df = self.kl_store.load(code, period="1d")
        except FileNotFoundError:
            raw = self.tq.get_market_data(
                field_list=[], stock_list=[code], period="1d",
                count=24000, dividend_type="front")
            fields = ["Open", "High", "Low", "Close", "Volume", "Amount"]
            parts = []
            for f in fields:
                sub = self.tq.price_df(raw, f, column_names=[code])
                if not sub.empty:
                    parts.append(sub.rename(columns={code: f}))
            if not parts:
                return {"error": "无法获取数据"}
            import pandas as pd
            df = pd.concat(parts, axis=1)
            df.index = pd.to_datetime(df.index.astype(str))

        close = df["Close"]
        ma5  = close.rolling(5).mean()
        ma20 = close.rolling(20).mean()
        ema12 = close.ewm(span=12, adjust=False).mean()
        ema26 = close.ewm(span=26, adjust=False).mean()
        dif = ema12 - ema26
        dea = dif.ewm(span=9, adjust=False).mean()
        macd_bar = (dif - dea) * 2

        last = float(close.iloc[-1])
        score = 0
        score += 2 if last > float(ma5.iloc[-1]) else -2
        score += 2 if float(ma5.iloc[-1]) > float(ma20.iloc[-1]) else -2
        score += 1 if float(macd_bar.iloc[-1]) > 0 else -1

        trend = "多头" if last > float(ma5.iloc[-1]) > float(ma20.iloc[-1]) else \
                "空头" if last < float(ma5.iloc[-1]) < float(ma20.iloc[-1]) else "震荡"
        conclusion = "偏多 建议持有或买入" if score >= 2 else \
                     "偏空 建议观望或减仓" if score <= -2 else "中性 观望为主"

        crosses = []
        golden = (ma5.shift(1) <= ma20.shift(1)) & (ma5 > ma20)
        death  = (ma5.shift(1) >= ma20.shift(1)) & (ma5 < ma20)
        for i in range(len(df)):
            if golden.iloc[i]:
                crosses.append({"date": df.index[i].strftime("%Y-%m-%d"),
                                "signal": "MA金叉", "price": round(float(close.iloc[i]), 2)})
            if death.iloc[i]:
                crosses.append({"date": df.index[i].strftime("%Y-%m-%d"),
                                "signal": "MA死叉", "price": round(float(close.iloc[i]), 2)})

        try:
            info = self.tq.get_stock_info(stock_code=code) or {}
            name = info.get("Name") or code
        except Exception:
            name = code

        return {
            "code": code, "name": name, "last_price": round(last, 2),
            "ma5": round(float(ma5.iloc[-1]), 2),
            "ma20": round(float(ma20.iloc[-1]), 2),
            "dif": round(float(dif.iloc[-1]), 4),
            "dea": round(float(dea.iloc[-1]), 4),
            "macd_bar": round(float(macd_bar.iloc[-1]), 4),
            "ma_trend": trend, "score": score, "conclusion": conclusion,
            "recent_crosses": crosses[-8:],
        }

    # ================================================================
    # 交易目标售价计算
    # ================================================================
    def predict_sell(self, user_id, code, target_r,
                     add_qty=0, add_price=None,
                     manual_n1=None, manual_a=None):
        """交易预测 —— 根据目标收益率反推售价

        模式1 (add_qty=0):  B = A × (1+C)
        模式2 (add_qty>0):  B₂ = (N₁·A + N₂·B₁) × (1+C) / (N₁+N₂)

        参数:
          user_id   — PortfolioStore 用户 ID
          code      — 股票代码
          target_r  — 目标收益率 (小数, e.g. 0.15 表示 15%)
          add_qty   — 加仓数量 (0=不加仓)
          add_price — 加仓价格 (None=取现价)
          manual_n1 — 手动输入持仓数量 (无 DB 持仓时用于虚拟预测)
          manual_a  — 手动输入持仓成本价

        返回: dict (直接 jsonify)
        """
        if self.portfolio is None or self.tq is None:
            raise RuntimeError("PredictService 缺少依赖: portfolio / tq_client 未注入")

        pos_df = self.portfolio.positions(user_id)
        row = pos_df[pos_df["code"] == code]

        if not row.empty:
            n1 = int(row.iloc[0]["quantity"])
            a = float(row.iloc[0]["cost_price"])
            from_pos = True
        elif manual_n1 is not None and manual_a is not None:
            n1 = int(manual_n1)
            a = float(manual_a)
            from_pos = False
        else:
            return {
                "error": f"无持仓: {code}",
                "hint": "可在前端手动输入数量和成本价进行虚拟预测"
            }

        try:
            snap = self.tq.get_market_snapshot(stock_code=code) or {}
            b1 = float(snap.get("Now") or snap.get("LastClose") or a)
            info = self.tq.get_stock_info(stock_code=code) or {}
            name = info.get("Name") or (row.iloc[0].get("name") if not row.empty else None) or code
        except Exception:
            b1 = a
            name = code

        base = {
            "code": code, "name": name,
            "n1": n1, "a": round(a, 3), "b1": round(b1, 3),
            "target_r_pct": round(target_r * 100, 2),
            "from_portfolio": from_pos,
        }

        if add_qty > 0:
            if add_price is None:
                add_price = b1
            n2 = add_qty
            b2 = (n1 * a + n2 * add_price) * (1 + target_r) / (n1 + n2)
            new_avg_cost = (n1 * a + n2 * add_price) / (n1 + n2)
            rise_from_now = (b2 / b1 - 1) * 100 if b1 > 0 else 0
            rise_from_new = (b2 / new_avg_cost - 1) * 100 if new_avg_cost > 0 else 0
            return {
                **base,
                "mode": "add",
                "formula": "B₂ = (N₁·A + N₂·B₁) × (1+C) / (N₁+N₂)",
                "n2": n2, "add_price": round(add_price, 3),
                "new_avg_cost": round(new_avg_cost, 3),
                "b2": round(b2, 3),
                "rise_from_now_pct": round(rise_from_now, 2),
                "rise_from_new_avg_pct": round(rise_from_new, 2),
                "total_shares": n1 + n2,
                "add_amount": round(n2 * add_price, 2),
            }
        else:
            b = a * (1 + target_r)
            rise_from_now = (b / b1 - 1) * 100 if b1 > 0 else 0
            return {
                **base,
                "mode": "noadd",
                "formula": "B = A × (1+C)",
                "b": round(b, 3),
                "rise_from_now_pct": round(rise_from_now, 2),
            }
