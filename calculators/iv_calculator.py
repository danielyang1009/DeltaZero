"""
Black-76 隐含波动率求解引擎。

采用"隐含远期价格（Implied Forward）+ Black-76"框架，
规避 A 股市场融券成本高昂与股息率（q）难以估计的问题：
  - 不使用现货价格 S 与股息率 q
  - 通过平值期权 Put-Call Parity 倒算 F，F 已内化股息、借券成本与基差预期
  - 以 Black-76 模型对所有行权价求 IV

典型调用流程：
    F = calc_implied_forward(K_atm, C_mid, P_mid, T, r)
    iv = calc_iv_black76(F, K, T, r, mid_price, option_type)
"""

from __future__ import annotations

import math

from scipy.optimize import brentq
from scipy.stats import norm


# ──────────────────────────────────────────────────────────
# Step 1: 隐含远期价格
# ──────────────────────────────────────────────────────────

def calc_implied_forward(
    K_atm: float,
    C_mid: float,
    P_mid: float,
    T: float,
    r: float,
) -> float:
    """
    通过平值期权 Put-Call Parity 倒算隐含远期价格 F。

        F = K_atm + (C_mid - P_mid) * exp(r * T)

    该 F 已内化股息、借券成本与基差预期，替代 S 和 q 作为后续定价锚。

    Args:
        K_atm:  平值行权价
        C_mid:  平值认购中间价 (bid+ask)/2
        P_mid:  平值认沽中间价 (bid+ask)/2
        T:      剩余到期时间（年化）
        r:      无风险利率（连续复利）
    """
    return K_atm + (C_mid - P_mid) * math.exp(r * T)


# ──────────────────────────────────────────────────────────
# Step 2: Black-76 定价与 IV 求解
# ──────────────────────────────────────────────────────────

def black76_price(
    F: float,
    K: float,
    T: float,
    r: float,
    sigma: float,
    option_type: str,
) -> float:
    """
    Black-76 欧式期权理论价格。

        d1 = [ln(F/K) + (σ²/2)·T] / (σ√T)
        d2 = d1 - σ√T
        Call = e^{-rT} · [F·N(d1) - K·N(d2)]
        Put  = e^{-rT} · [K·N(-d2) - F·N(-d1)]

    Args:
        option_type: 'C'/'CALL' 或 'P'/'PUT'
    """
    sqrt_T = math.sqrt(T)
    d1 = (math.log(F / K) + 0.5 * sigma * sigma * T) / (sigma * sqrt_T)
    d2 = d1 - sigma * sqrt_T
    disc = math.exp(-r * T)
    if option_type.upper() in ("C", "CALL"):
        return disc * (F * norm.cdf(d1) - K * norm.cdf(d2))
    else:
        return disc * (K * norm.cdf(-d2) - F * norm.cdf(-d1))


def calc_iv_black76(
    F: float,
    K: float,
    T: float,
    r: float,
    price: float,
    option_type: str,
    tol: float = 1e-6,
) -> float:
    """
    Black-76 隐含波动率求解（Brent 法）。

    Brent 法在深度虚值期权（Vega 极小）场景下比 Newton-Raphson 更稳健。
    搜索区间 σ ∈ [1e-4, 5.0]（对应 0.01% ~ 500% 年化波动率）。

    Args:
        F:           隐含远期价格（由 calc_implied_forward 得到）
        K:           行权价
        T:           剩余到期时间（年化，> 0）
        r:           无风险利率（连续复利）
        price:       期权中间价（市场价格）
        option_type: 'C'/'CALL' 或 'P'/'PUT'
        tol:         收敛精度

    Returns:
        隐含波动率（float），无解时返回 nan。
    """
    if price <= 0 or T <= 0 or F <= 0 or K <= 0:
        return float("nan")

    # 内在价值下界：price 低于折现内在价值则无解
    disc = math.exp(-r * T)
    if option_type.upper() in ("C", "CALL"):
        intrinsic = max(disc * (F - K), 0.0)
    else:
        intrinsic = max(disc * (K - F), 0.0)

    if price < intrinsic - tol:
        return float("nan")

    def obj(sigma: float) -> float:
        return black76_price(F, K, T, r, sigma, option_type) - price

    lo, hi = 1e-4, 5.0
    try:
        f_lo = obj(lo)
        f_hi = obj(hi)
        if f_lo * f_hi > 0:
            return float("nan")
        return float(brentq(obj, lo, hi, xtol=tol, maxiter=200))
    except Exception:
        return float("nan")
