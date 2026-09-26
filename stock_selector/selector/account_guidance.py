"""Account-aware share budgets. Strategy scores and frozen parameters are not changed."""
import math
import pandas as pd


def validate_account(account):
    out = dict(account)
    for key in ("equity", "cash", "max_exposure", "max_industry", "cost_buffer"):
        out[key] = float(out[key])
        if not math.isfinite(out[key]):
            raise ValueError("账户设置必须是有限数值")
    if out["equity"] <= 0 or not 0 <= out["cash"] <= out["equity"]:
        raise ValueError("总资产应大于0，现金应介于0和总资产之间")
    if not 0 < out["max_industry"] <= out["max_exposure"] <= 1:
        raise ValueError("仓位比例需满足：0 < 行业上限 <= 总上限 <= 100%")
    if not 0 <= out["cost_buffer"] <= .1:
        raise ValueError("费用及价格缓冲比例应介于0和10%")
    pd.Timestamp(out["as_of"])
    return out


def build_account_guidance(plan, holdings, scored, quotes, account, as_of, expected_price_date):
    result = plan.copy()
    result["suggested_quantity"] = 0
    result["estimated_amount"] = 0.0
    result["estimated_stop_risk"] = 0.0
    result["guidance"] = "请先填写账户设置并核对持仓"
    result["valid_as_of"] = as_of
    if not account:
        return result
    a = validate_account(account)
    if a["as_of"] != as_of:
        result["guidance"] = "账户余额日期与本次节点不一致，请重新核对"
        return result
    equity, cash = a["equity"], a["cash"]
    industries = scored.set_index("ticker")["industry_l1"].to_dict()
    held, exposure = set(), {}
    for h in holdings.to_dict("records"):
        code = str(h["ticker"])
        held.add(code)
        q = quotes.get(code, {})
        if (code not in industries or q.get("error") or q.get("basis_changed", True)
                or q.get("price_as_of") != expected_price_date or q.get("price_basis") != "unadjusted"):
            result["guidance"] = "已有持仓的行情、行业或成本口径不完整，请先复核"
            return result
        value = float(q["price"]) * float(h["quantity"])
        if not math.isfinite(value) or value < 0:
            raise ValueError("持仓市值无效")
        industry = industries[code]
        exposure[industry] = exposure.get(industry, 0) + value
    if abs(sum(exposure.values()) + cash - equity) > max(1, equity * .01):
        result["guidance"] = "持仓市值+现金与总资产偏差超过1%，请核对完整账户"
        return result
    available = min(cash, max(0, equity * a["max_exposure"] - sum(exposure.values())), equity * .30)
    for i, row in result.iterrows():
        code, industry = str(row["ticker"]), row["industry_l1"]
        if code in held:
            result.at[i, "guidance"] = "已有持仓，本版不自动加仓"
            continue
        if row["entry_delay_sessions"] > 0:
            result.at[i, "guidance"] = "等待过热期结束后重新运行"
            continue
        price = float(row.get("execution_price", float("nan")))
        if not math.isfinite(price) or price <= 0 or row.get("execution_price_as_of") != expected_price_date:
            result.at[i, "guidance"] = "缺少最新未复权参考价"
            continue
        budget = min(available, equity * .06, max(0, equity * a["max_industry"] - exposure.get(industry, 0)))
        unit = price * (1 + a["cost_buffer"])
        quantity = math.floor(budget / unit)
        # STAR: minimum 200 shares, increments of one thereafter; other supported A shares: 100.
        quantity = (quantity if quantity >= 200 else 0) if code.startswith("sh.688") else quantity // 100 * 100
        amount = quantity * price
        reserved = quantity * unit
        result.loc[i, ["suggested_quantity", "estimated_amount", "estimated_stop_risk"]] = [quantity, amount, amount * .1]
        result.at[i, "guidance"] = "可考虑建仓；成交价变化时重新计算" if quantity else "余额或仓位额度不足最小买入单位"
        available -= reserved
        exposure[industry] = exposure.get(industry, 0) + reserved
    return result
