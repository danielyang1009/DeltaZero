# -*- coding: utf-8 -*-
"""calculators/vectorized_pricer.py — Black-76 IV 求解器（Brent 法，绝对稳健）。

两条金工容错机制（见代码内注释）：
  [GUARD-1] 边界违规布尔掩码：price<=0 / not finite / K<=0 / price<intrinsic-1e-4 → nan，跳过 brentq
  [GUARD-2] T 毫秒级动态对齐：time.time() Unix 时间戳 + T<=0 拦截（max(T,1e-6)）
"""
from __future__ import annotations

import math
import time
import numpy as np
from scipy.optimize import brentq
from scipy.special import erf as _erf

_SQRT2   = math.sqrt(2.0)
_SQRT2PI = math.sqrt(2.0 * math.pi)

# 年秒数（儒略年）
_SECS_PER_YEAR = 31_557_600.0


def _ncdf(x: np.ndarray) -> np.ndarray:
    return 0.5 * (1.0 + _erf(x / _SQRT2))


def _npdf(x: np.ndarray) -> np.ndarray:
    return np.exp(-0.5 * x * x) / _SQRT2PI


def _ncdf_scalar(x: float) -> float:
    return 0.5 * math.erfc(-x / _SQRT2)


class VectorizedIVCalculator:
    """Black-76 IV 求解器（Brent 法，绝对稳健，深度虚值期权不再 nan）。"""

    def __init__(self, n_iter: int = 12, tol: float = 5e-5):
        # n_iter / tol 保留签名兼容，brentq 内部控制收敛，不使用这两个参数
        self.n_iter = n_iter
        self.tol    = tol

    # ──────────────────────────────────────────────────────────────
    # [GUARD-2] T 的毫秒级计算
    # ──────────────────────────────────────────────────────────────
    @staticmethod
    def calc_T(expiry_timestamp: float) -> float:
        """
        [GUARD-2] 用 time.time() 计算毫秒精度的年化剩余时间。

        T = (ExpiryTimestamp - time.time()) / 31_557_600
        返回 max(T, 1e-6)：防止 T<=0 导致 sqrt(T) 产生无效值。
        """
        T_raw = (expiry_timestamp - time.time()) / _SECS_PER_YEAR
        return max(T_raw, 1e-6)

    def calc_iv(
        self,
        F: float,
        K_arr: np.ndarray,       # shape (N,) 行权价
        T: float,                # 年化剩余时间（由 calc_T 得到，已保证 > 0）
        r: float,
        price_arr: np.ndarray,   # shape (N,) 市场中间价
        flag_arr: np.ndarray,    # shape (N,) +1=call, -1=put
    ) -> np.ndarray:
        """
        批量求 Black-76 隐含波动率（Brent 法逐合约求解）。

        Returns:
            iv_arr shape (N,)，无效/端点同号 → nan。
        """
        disc   = math.exp(-r * T)
        sqrt_T = math.sqrt(T)
        K_safe = np.where(K_arr > 0, K_arr, 1.0)

        # ── [GUARD-1] 边界违规过滤（布尔掩码） ──────────────────
        intrinsic_call = disc * np.maximum(F - K_safe, 0.0)
        intrinsic_put  = disc * np.maximum(K_safe - F, 0.0)
        intrinsic      = np.where(flag_arr > 0, intrinsic_call, intrinsic_put)

        valid = (
            (price_arr > 0)
            & np.isfinite(price_arr)
            & (K_arr > 0)
            & (price_arr >= intrinsic - 1e-4)   # 1e-4 容许 DDE 报价噪声
        )

        log_FK_arr = np.log(F / K_safe)

        iv_list: list[float] = []
        for i in range(len(K_arr)):
            if not valid[i]:
                iv_list.append(float("nan"))
                continue

            K_i       = float(K_safe[i])
            price_i   = float(price_arr[i])
            flag_i    = float(flag_arr[i])
            log_FK_i  = float(log_FK_arr[i])

            def obj(sigma: float) -> float:
                d1 = (log_FK_i + 0.5 * sigma * sigma * T) / (sigma * sqrt_T)
                d2 = d1 - sigma * sqrt_T
                if flag_i > 0:
                    th = disc * (F * _ncdf_scalar(d1) - K_i * _ncdf_scalar(d2))
                else:
                    th = disc * (K_i * _ncdf_scalar(-d2) - F * _ncdf_scalar(-d1))
                return th - price_i

            try:
                f_lo = obj(1e-4)
                f_hi = obj(5.0)
                if f_lo * f_hi >= 0:
                    iv_list.append(float("nan"))
                else:
                    iv_list.append(brentq(obj, 1e-4, 5.0, xtol=1e-6, maxiter=200))
            except Exception:
                iv_list.append(float("nan"))

        return np.array(iv_list)

    def calc_greeks(
        self,
        F: float,
        K_arr: np.ndarray,
        T: float,
        r: float,
        sigma_arr: np.ndarray,
        flag_arr: np.ndarray,
    ) -> dict:
        """向量化计算 Delta / Gamma / Vega / Theta（Black-76）。"""
        K_safe  = np.where(K_arr > 0, K_arr, 1.0)
        disc    = math.exp(-r * T)
        sqrt_T  = math.sqrt(T)
        log_FK  = np.log(F / K_safe)
        sig_sqT = sigma_arr * sqrt_T
        # [GUARD-2] Greeks 计算中同样保护分母
        safe_sig_sqT = np.maximum(sig_sqT, 1e-8)
        d1      = (log_FK + 0.5 * sigma_arr ** 2 * T) / safe_sig_sqT
        d2      = d1 - sig_sqT
        nd1, nd2 = _ncdf(d1), _ncdf(d2)
        npd1    = _npdf(d1)
        vega    = F * disc * npd1 * sqrt_T
        delta   = np.where(flag_arr > 0, disc * nd1, disc * (nd1 - 1.0))
        gamma   = disc * npd1 / np.maximum(F * safe_sig_sqT, 1e-8)
        theta_call = (-F * disc * npd1 * sigma_arr / (2 * sqrt_T)
                      + r * disc * (F * nd1 - K_safe * nd2))
        theta_put  = (-F * disc * npd1 * sigma_arr / (2 * sqrt_T)
                      + r * disc * (K_safe * (1.0 - nd2) - F * (1.0 - nd1)))
        theta   = np.where(flag_arr > 0, theta_call, theta_put)
        return {"delta": delta, "gamma": gamma, "vega": vega, "theta": theta}
