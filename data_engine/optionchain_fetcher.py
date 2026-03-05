# -*- coding: utf-8 -*-
"""
开盘前从 Wind 抓取期权链（含合约乘数），保存到 metadata 目录。

用法:
    python -m data_engine.optionchain_fetcher [--date YYYY-MM-DD]
    python -m data_engine.optionchain_fetcher --timeout 90 --retry 2

输出: metadata/YYYY-MM-DD_optionchain.csv
"""

from __future__ import annotations

import argparse
import logging
import sys
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from datetime import date, datetime
from pathlib import Path

from config.settings import UNDERLYINGS
from utils.wind_helpers import wind_connect

logger = logging.getLogger(__name__)

# Wind 错误码 -40521010 表示网络超时
WIND_ERROR_NETWORK_TIMEOUT = -40521010

PROJECT_ROOT = Path(__file__).resolve().parent.parent
METADATA_DIR = PROJECT_ROOT / "metadata"


def _errmsg(err: int) -> str:
    if err == WIND_ERROR_NETWORK_TIMEOUT:
        return "网络超时 (Network Timeout)"
    return f"ErrorCode={err}"


def fetch_optionchain_from_wind(
    target_date: date,
    underlyings: list[str] | None = None,
    timeout_sec: int = 60,
    retry: int = 1,
):
    """
    从 Wind wset 抓取期权链，包含合约乘数等信息。

    Args:
        target_date: 查询日期
        underlyings: 标的代码列表，默认 50/300/500 ETF
        timeout_sec: 单次请求超时秒数，超时则报错或重试
        retry: 失败/超时后的重试次数（0=不重试）

    Returns:
        pandas DataFrame，多标的合并去重
    """
    try:
        from WindPy import w
        import pandas as pd
    except ImportError as e:
        raise RuntimeError(f"依赖缺失: {e}") from e

    if underlyings is None:
        underlyings = UNDERLYINGS

    date_str = target_date.strftime("%Y-%m-%d")
    dfs = []

    if not wind_connect(w, timeout=30, retries=3, delay_secs=2.0, logger=logger):
        raise RuntimeError("Wind 连接失败")

    def _fetch_one(opts: str) -> pd.DataFrame | None:
        """单次请求，带 usedf。若 TypeError 则回退到非 usedf 格式。"""
        try:
            raw = w.wset("optionchain", opts, usedf=True)
        except TypeError:
            res = w.wset("optionchain", opts)
            if res is None or res.ErrorCode != 0:
                return None
            fields = getattr(res, "Fields", []) or []
            data = getattr(res, "Data", []) or []
            if not data or not fields:
                return None
            return pd.DataFrame(
                {fields[j]: data[j] for j in range(min(len(fields), len(data)))}
            )
        if raw is None:
            return None
        if isinstance(raw, tuple):
            err, df = raw[0], raw[1]
            if err != 0 or df is None or (hasattr(df, "empty") and df.empty):
                raise _WindError(err)
            return df
        if raw is None or (hasattr(raw, "empty") and raw.empty):
            return None
        return raw

    class _WindError(Exception):
        def __init__(self, err: int):
            self.err = err
            super().__init__(_errmsg(err))

    total = len(underlyings)
    try:
        for idx, us_code in enumerate(underlyings):
            print(f"PROGRESS:{idx}/{total}:{us_code}", flush=True)
            opts = f"date={date_str};us_code={us_code};option_var=all;call_put=all"
            df = None
            last_err: str | None = None

            for attempt in range(retry + 1):
                try:
                    with ThreadPoolExecutor(max_workers=1) as ex:
                        fut = ex.submit(_fetch_one, opts)
                        df = fut.result(timeout=timeout_sec)
                except FuturesTimeoutError:
                    last_err = f"请求超时（{timeout_sec}s 内无响应）"
                    logger.warning("wset optionchain %s: us_code=%s", last_err, us_code)
                    if attempt < retry:
                        logger.info("重试 %s (%d/%d)", us_code, attempt + 2, retry + 1)
                    continue
                except _WindError as e:
                    last_err = str(e)
                    logger.warning("wset optionchain 失败: us_code=%s %s", us_code, last_err)
                    if attempt < retry and e.err == WIND_ERROR_NETWORK_TIMEOUT:
                        logger.info("网络超时，重试 %s (%d/%d)", us_code, attempt + 2, retry + 1)
                        continue
                    continue
                except Exception as e:
                    last_err = str(e)
                    logger.warning("抓取 %s 期权链异常: %s", us_code, e)
                    if attempt < retry:
                        logger.info("重试 %s (%d/%d)", us_code, attempt + 2, retry + 1)
                    continue

                if df is not None and (not hasattr(df, "empty") or not df.empty):
                    dfs.append(df)
                else:
                    last_err = "返回空数据"
                break
            else:
                if last_err:
                    logger.warning("跳过 %s（%s，已重试 %d 次）", us_code, last_err, retry)
    finally:
        try:
            w.stop()
        except Exception:
            pass

    print(f"PROGRESS:{total}/{total}:done", flush=True)

    if not dfs:
        return pd.DataFrame()

    merged = pd.concat(dfs, ignore_index=True)
    # 按期权合约代码去重（同一合约可能出现在多标的结果中）
    code_col = next(
        (c for c in ("option_code", "wind_code", "code") if c in merged.columns),
        None,
    )
    if code_col:
        merged = merged.drop_duplicates(subset=[code_col], keep="first")
    return merged


def main() -> int:
    parser = argparse.ArgumentParser(
        description="开盘前从 Wind 抓取期权链（含合约乘数），保存到 metadata",
    )
    parser.add_argument(
        "--date",
        type=str,
        default=None,
        help="查询日期 YYYY-MM-DD，默认今日",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=60,
        help="单次请求超时秒数（默认 60），超时或 -40521010 网络超时时会重试",
    )
    parser.add_argument(
        "--retry",
        type=int,
        default=1,
        help="失败/超时后的重试次数（默认 1）",
    )
    args = parser.parse_args()

    if args.date:
        try:
            target_date = datetime.strptime(args.date, "%Y-%m-%d").date()
        except ValueError:
            print(f"无效日期格式: {args.date}，应为 YYYY-MM-DD")
            return 1
    else:
        target_date = date.today()

    METADATA_DIR.mkdir(parents=True, exist_ok=True)
    out_path = METADATA_DIR / f"{target_date.strftime('%Y-%m-%d')}_optionchain.csv"

    print(f"正在从 Wind 抓取 {target_date} 期权链（超时 {args.timeout}s，重试 {args.retry} 次）...")
    try:
        df = fetch_optionchain_from_wind(
            target_date,
            timeout_sec=args.timeout,
            retry=args.retry,
        )
    except RuntimeError as e:
        print(f"抓取失败: {e}")
        return 1

    if df is None or (hasattr(df, "empty") and df.empty):
        print("未获取到任何合约数据")
        return 1

    df.to_csv(out_path, index=False, encoding="utf-8-sig")
    n = len(df)
    print(f"已保存 {n} 条至 {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
