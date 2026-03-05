#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
债券期限结构抓取器（Shibor + 中债国债收益率曲线）。

数据来源：
    - Shibor：中国货币网 chinamoney.com.cn ShiborHis 接口
    - 中债国债：中债官网 yield.chinabond.com.cn 「标准期限信息下载(excel)」

用法示例（命令行）：
    python -m data_engine.bond_termstructure_fetcher --kind all
    python -m data_engine.bond_termstructure_fetcher --kind shibor --date 2026-03-05

输出（默认，横表格式）：
    D:\\MARKET_DATA\\macro\\shibor\\shibor_yieldcurve_YYYYMMDD.csv
    D:\\MARKET_DATA\\macro\\cgb_yield\\cgb_yieldcurve_YYYYMMDD.csv
"""

from __future__ import annotations

import argparse
import csv
import logging
from datetime import date, datetime
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests
import zipfile
import xml.etree.ElementTree as ET

from config.settings import DEFAULT_MARKET_DATA_DIR
from utils.time_utils import bj_today

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 1. Shibor 抓取（中国货币网 ShiborHis 接口）
# ---------------------------------------------------------------------------

_SHIBOR_TENORS = ["O/N", "1W", "2W", "1M", "3M", "6M", "9M", "1Y"]
_SHIBOR_API_TO_OUT = {"ON": "O/N", "1W": "1W", "2W": "2W", "1M": "1M", "3M": "3M", "6M": "6M", "9M": "9M", "1Y": "1Y"}


def fetch_shibor(target_date: Optional[date] = None) -> Dict[str, Any]:
    """
    从中国货币网 ShiborHis 接口抓取 Shibor 期限结构，返回横表单行数据。
    注：ShiborTxt 已 404，改用 ShiborHis。
    """
    d = target_date or bj_today()
    date_str = d.strftime("%Y-%m-%d")
    out: Dict[str, Any] = {"date": date_str, **{t: None for t in _SHIBOR_TENORS}}

    url = "https://www.chinamoney.com.cn/ags/ms/cm-u-bk-shibor/ShiborHis"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Referer": "https://www.chinamoney.com.cn/chinese/bkshibor/",
    }
    try:
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        data = response.json()
    except Exception as e:
        raise RuntimeError(f"Shibor 请求失败: {e}") from e

    records = data.get("records", [])
    if not records:
        logger.warning("ShiborHis 未返回数据")
        return out

    row = None
    for r in records:
        if r.get("showDateCN") == date_str:
            row = r
            break
    if row is None:
        row = records[0]

    for api_key, out_key in _SHIBOR_API_TO_OUT.items():
        val = row.get(api_key)
        if val is not None:
            try:
                out[out_key] = round(float(val), 4)
            except (TypeError, ValueError):
                out[out_key] = None
    return out


# ---------------------------------------------------------------------------
# 2. 中债国债收益率曲线抓取（中债官网标准期限 Excel）
# ---------------------------------------------------------------------------

_CGB_FULL_TENORS = [
    0.0, 0.08, 0.17, 0.25, 0.5, 0.75, 1.0, 2.0, 3.0,
    5.0, 7.0, 10.0, 15.0, 20.0, 30.0, 40.0, 50.0,
]
_CGB_TENOR_COLS = [f"{t}y" for t in _CGB_FULL_TENORS]

_CGB_EXCEL_URL = "https://yield.chinabond.com.cn/cbweb-mn/yc/downBzqxDetail"
_CGB_EXCEL_BASE_PARAMS = {
    # 对应“中债国债收益率曲线(到期)”的定义 ID
    "ycDefIds": "2c9081e50a2f9606010a3068cae70001",
    "zblx": "txy",
    "dxbj": "0",
    "qxlx": "0",
    "yqqxN": "N",
    "yqqxK": "K",
    "wrjxCBFlag": "0",
    "locale": "",
}


def _download_cgb_excel_bytes(target_date: date) -> bytes:
    """
    直接调用 downBzqxDetail 下载标准期限 Excel，返回原始字节流。
    """
    params = dict(_CGB_EXCEL_BASE_PARAMS)
    params["workTime"] = target_date.strftime("%Y-%m-%d")

    # 简单 UA 即可；经验证无需 Cookie 也能下载
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/145.0.0.0 Safari/537.36"
        ),
        "Accept": "application/octet-stream,application/vnd.ms-excel,application/x-msdownload,*/*;q=0.1",
        "Referer": "https://yield.chinabond.com.cn/cbweb-mn/yield_main?locale=zh_CN",
    }
    resp = requests.get(_CGB_EXCEL_URL, params=params, headers=headers, timeout=15)
    resp.raise_for_status()
    return resp.content


def _parse_cgb_excel(content: bytes) -> Dict[str, float]:
    """
    解析中债标准期限 Excel，返回 tenor->yield 映射（如 {'0.08y': 1.2331, ...}）。

    为避免额外依赖 openpyxl，这里直接解析 sheet1.xml。
    """
    ns = {"main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    with zipfile.ZipFile(BytesIO(content)) as zf:
        with zf.open("xl/worksheets/sheet1.xml") as f:
            root = ET.parse(f).getroot()

    result: Dict[str, float] = {}
    for row in root.findall(".//main:row", ns):
        cells = row.findall("main:c", ns)
        if len(cells) < 3:
            continue

        def _cell_text(cell) -> Optional[str]:
            t = cell.get("t")
            if t == "inlineStr":
                is_node = cell.find("main:is/main:t", ns)
                return is_node.text.strip() if is_node is not None and is_node.text else None
            v = cell.find("main:v", ns)
            return v.text.strip() if v is not None and v.text else None

        tenor_text = _cell_text(cells[1])
        y_text = _cell_text(cells[2])
        if not tenor_text or not y_text:
            continue

        try:
            # 仅用于校验是数字；真实标签仍用文本
            float(tenor_text)
            y_val = float(y_text)
        except ValueError:
            # 跳过表头等非数值行
            continue

        key = f"{tenor_text}y"
        result[key] = round(y_val, 4)

    return result


def fetch_cgb_yieldcurve(target_date: Optional[date] = None) -> Dict[str, Any]:
    """
    通过中债官网「标准期限信息下载(excel)」抓取完整国债收益率曲线（17 个标准期限）。

    Args:
        target_date: 目标日期；未指定则使用 bj_today()
    Returns:
        {"date": "YYYY-MM-DD", "0.0y": ..., "0.08y": ..., ..., "50y": ...}
    """
    d = target_date or bj_today()
    date_str = d.strftime("%Y-%m-%d")
    out: Dict[str, Any] = {"date": date_str, **{f"{t}y": None for t in _CGB_FULL_TENORS}}

    try:
        content = _download_cgb_excel_bytes(d)
        tenor_to_yield = _parse_cgb_excel(content)
        if not tenor_to_yield:
            logger.warning("中债标准期限 Excel 未解析出任何收益率数据")
            return out
        for k, v in tenor_to_yield.items():
            if k in out:
                out[k] = v
    except Exception as e:
        logger.warning("中债国债收益率曲线抓取失败: %s", e)
    return out


# ---------------------------------------------------------------------------
# 落盘函数
# ---------------------------------------------------------------------------


def _ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def save_shibor_daily(target_date: date, base_dir: Optional[Path] = None) -> Path:
    """保存 Shibor 期限结构为横表 CSV：date,O/N,1W,2W,1M,3M,6M,9M,1Y"""
    base = base_dir or Path(DEFAULT_MARKET_DATA_DIR)
    out_dir = base / "macro" / "shibor"
    _ensure_dir(out_dir)
    fname = f"shibor_yieldcurve_{target_date.strftime('%Y%m%d')}.csv"
    out_path = out_dir / fname

    row = fetch_shibor(target_date)
    cols = ["date"] + _SHIBOR_TENORS
    with out_path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(cols)
        writer.writerow([row.get(c) if c == "date" else row.get(c, "") for c in cols])
    return out_path


def save_cgb_yieldcurve_daily(target_date: date, base_dir: Optional[Path] = None) -> Path:
    """保存中债国债收益率曲线为横表 CSV：date,0.0y,0.08y,...,50y（共 17 个期限）"""
    base = base_dir or Path(DEFAULT_MARKET_DATA_DIR)
    out_dir = base / "macro" / "cgb_yield"
    _ensure_dir(out_dir)
    fname = f"cgb_yieldcurve_{target_date.strftime('%Y%m%d')}.csv"
    out_path = out_dir / fname

    row = fetch_cgb_yieldcurve(target_date)
    cols = ["date"] + _CGB_TENOR_COLS
    with out_path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(cols)
        writer.writerow([row.get(c) if c == "date" else row.get(c, "") for c in cols])
    return out_path


# ---------------------------------------------------------------------------
# CLI 入口
# ---------------------------------------------------------------------------


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="从 Shibor 与中债网站抓取当日无风险利率期限结构，并保存至 D:\\MARKET_DATA\\macro",
    )
    parser.add_argument(
        "--date",
        type=str,
        default=None,
        help="目标日期 YYYY-MM-DD，默认使用北京时间今日",
    )
    parser.add_argument(
        "--kind",
        type=str,
        choices=["shibor", "cgb", "all"],
        default="all",
        help="抓取品种：shibor / cgb / all（默认 all）",
    )
    parser.add_argument(
        "--base-dir",
        type=str,
        default=str(DEFAULT_MARKET_DATA_DIR),
        help="输出根目录，默认使用 config.settings.DEFAULT_MARKET_DATA_DIR",
    )
    args = parser.parse_args(argv)

    if args.date:
        try:
            target_date = datetime.strptime(args.date, "%Y-%m-%d").date()
        except ValueError:
            print(f"无效日期格式: {args.date}，应为 YYYY-MM-DD")
            return 1
    else:
        target_date = bj_today()

    base_dir = Path(args.base_dir)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    try:
        if args.kind in ("shibor", "all"):
            path = save_shibor_daily(target_date, base_dir=base_dir)
            print(f"Shibor 数据已保存至: {path}")
        if args.kind in ("cgb", "all"):
            path = save_cgb_yieldcurve_daily(target_date, base_dir=base_dir)
            print(f"中债收益率曲线数据已保存至: {path}")
    except Exception as exc:
        print(f"抓取失败: {exc}")
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
