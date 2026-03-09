#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""DeltaZero Web 控制台（统一监控 + 调度）。"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, Optional
import threading
import time

import psutil
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from config.settings import DEFAULT_MARKET_DATA_DIR
from web.data_stats import (
    chunks_readable,
    count_today_chunks,
    get_fetch_state,
    launch_fetch_task,
    merge_status_readable,
    read_snapshot_stats,
    run_merge,
    snapshot_readable,
)
from web.process_manager import (
    arg_from_cmd,
    find_monitor_processes,
    find_recorder_processes,
    find_infinitrader_processes,
    _is_real_databus_proc,
    _is_real_monitor_proc,
    process_info,
    safe_cmdline,
    spawn_module,
)
from utils.time_utils import bj_now_naive, bj_today

TEMPLATE_PATH = Path(__file__).resolve().parent / "templates" / "index.html"
DDE_TEMPLATE_PATH = Path(__file__).resolve().parent / "templates" / "dde.html"

app = FastAPI(title="DeltaZero Web Console", version="0.3.0")
_dde_lock = threading.Lock()
_dde_feeder = None
_dde_routes_cache: Dict[str, Any] = {"key": "", "routes": {}}
_dde_health_cache: Dict[str, Any] = {
    "prev_values": {},
    "last_change_ts": {},
    "contract_status": {},
    "product_fused": {},
    "etf_prices": {},
    "timeout": 30.0,
}

_METADATA_DIR = Path(__file__).resolve().parent.parent / "metadata"
_WIND_OPTIONCHAIN_PATH = _METADATA_DIR / "wind_sse_optionchain.xlsx"
_wind_optionchain_cache: Dict[str, Any] = {}


def _excel_serial_to_date(serial) -> Optional[date]:
    """Excel date serial number -> date (1900-based)."""
    try:
        n = int(float(serial))
        if n < 1:
            return None
        from datetime import timedelta
        return date(1899, 12, 30) + timedelta(days=n)
    except (TypeError, ValueError):
        return None


def _load_wind_optionchain() -> Dict[str, Dict[str, Any]]:
    """
    解析 metadata/wind_*_optionchain.xlsx，返回 {合约代码(无后缀): {expiry_date, ...}}。
    跳过末尾 Wind 水印行。结果会缓存。
    """
    import re as _re
    import zipfile
    import xml.etree.ElementTree as ET

    if not _WIND_OPTIONCHAIN_PATH.exists():
        return {}

    fpath = str(_WIND_OPTIONCHAIN_PATH)
    cache_key = f"{fpath}:{_WIND_OPTIONCHAIN_PATH.stat().st_mtime}"
    if _wind_optionchain_cache.get("key") == cache_key:
        return _wind_optionchain_cache["data"]

    ns = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    col_re = _re.compile(r"([A-Z]+)")
    result: Dict[str, Dict[str, Any]] = {}

    try:
        zf = zipfile.ZipFile(fpath)
    except Exception:
        return result

    ss: list[str] = []
    if "xl/sharedStrings.xml" in zf.namelist():
        try:
            root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
            for si in root.findall(".//s:si", ns):
                parts = [t.text or "" for t in si.findall(".//s:t", ns)]
                ss.append("".join(parts))
        except Exception:
            pass

    for sheet_name in sorted(zf.namelist()):
        if not sheet_name.startswith("xl/worksheets/sheet") or not sheet_name.endswith(".xml"):
            continue
        try:
            root = ET.fromstring(zf.read(sheet_name))
        except Exception:
            continue
        rows = root.findall(".//s:sheetData/s:row", ns)
        for row in rows[1:]:
            cell_map: Dict[str, str] = {}
            for cell in row.findall("s:c", ns):
                ref = cell.get("r", "")
                m = col_re.match(ref)
                if not m:
                    continue
                col = m.group(1)
                t_attr = (cell.get("t") or "").strip()
                v_elem = cell.find("s:v", ns)
                val = v_elem.text if v_elem is not None and v_elem.text else ""
                if t_attr == "s" and val:
                    try:
                        val = ss[int(val)]
                    except (ValueError, IndexError):
                        pass
                cell_map[col] = val

            code_raw = cell_map.get("A", "").strip()
            if not code_raw or not code_raw[0].isdigit():
                continue

            code_bare = code_raw.replace(".SH", "").replace(".XSHG", "").strip()
            if not code_bare.isdigit():
                continue

            expiry_d = _excel_serial_to_date(cell_map.get("G", ""))
            result[code_bare] = {
                "expiry_date": expiry_d,
                "delivery_month": cell_map.get("H", ""),
            }
    zf.close()

    _wind_optionchain_cache["key"] = cache_key
    _wind_optionchain_cache["data"] = result
    return result


def _wind_optionchain_mtime_ago() -> str:
    """返回 wind_sse_optionchain.xlsx 距今多久未更新的描述文字。"""
    if not _WIND_OPTIONCHAIN_PATH.exists():
        return "文件不存在"
    mtime = _WIND_OPTIONCHAIN_PATH.stat().st_mtime
    delta = time.time() - mtime
    if delta < 60:
        return "刚刚"
    if delta < 3600:
        return f"{int(delta // 60)}分钟前"
    if delta < 86400:
        return f"{int(delta // 3600)}小时前"
    return f"{int(delta // 86400)}天前"


def _file_mtime_ago(path: Path) -> str:
    if not path.exists():
        return "未找到"
    delta = time.time() - path.stat().st_mtime
    if delta < 60:
        return "刚刚"
    if delta < 3600:
        return f"{int(delta // 60)}分钟前"
    if delta < 86400:
        return f"{int(delta // 3600)}小时前"
    return f"{int(delta // 86400)}天前"


def _wxy_50etf_mtime_ago() -> str:
    return _file_mtime_ago(_METADATA_DIR / "wxy_50etf.xlsx")


def _mtime_ago_str(mtime: float) -> str:
    """将 mtime 转为距今描述。"""
    delta = time.time() - mtime
    if delta < 60:
        return "刚刚"
    if delta < 3600:
        return f"{int(delta // 60)}分钟前"
    if delta < 86400:
        return f"{int(delta // 3600)}小时前"
    return f"{int(delta // 86400)}天前"


def _bond_files_info() -> Dict[str, Dict[str, Any]]:
    """返回 cgb_yieldcurve 与 shibor_yieldcurve 最新文件信息。"""
    base = Path(DEFAULT_MARKET_DATA_DIR)
    result: Dict[str, Dict[str, Any]] = {
        "cgb_yieldcurve": {"name": None, "mtime_ago": "文件不存在"},
        "shibor_yieldcurve": {"name": None, "mtime_ago": "文件不存在"},
    }
    for key, subdir, pattern in [
        ("cgb_yieldcurve", "macro/cgb_yield", "cgb_yieldcurve_*.csv"),
        ("shibor_yieldcurve", "macro/shibor", "shibor_yieldcurve_*.csv"),
    ]:
        dir_path = base / subdir
        if not dir_path.exists():
            continue
        files = sorted(dir_path.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)
        if files:
            latest = files[0]
            result[key] = {
                "name": latest.name,
                "mtime_ago": _mtime_ago_str(latest.stat().st_mtime),
            }
    return result


def _update_dde_health_from_rows(rows: list[Dict[str, Any]]) -> Dict[str, Any]:
    """
    依据当前轮询结果计算合约活跃度与品种熔断状态。
    这里复用与 DDESubscriber 相同的核心字段变化规则，避免页面侧静态值误判。
    """
    now = time.time()
    core_fields = ("last", "bid1", "ask1")
    timeout = float(_dde_health_cache.get("timeout", 30.0) or 30.0)
    prev_values: Dict[str, Dict[str, Any]] = _dde_health_cache["prev_values"]
    last_change_ts: Dict[str, float] = _dde_health_cache["last_change_ts"]
    contract_status: Dict[str, str] = _dde_health_cache["contract_status"]
    product_fused: Dict[str, bool] = _dde_health_cache["product_fused"]
    etf_prices: Dict[str, float] = _dde_health_cache["etf_prices"]

    # 合约状态更新
    for row in rows:
        code = str(row.get("code", "") or "").strip()
        if not code:
            continue
        cur = {k: row.get(k) for k in core_fields}
        prev = prev_values.get(code, {})
        changed = any(cur.get(k) != prev.get(k) for k in core_fields)
        if code not in last_change_ts:
            last_change_ts[code] = now
            contract_status[code] = "ACTIVE"
        elif changed:
            last_change_ts[code] = now
            contract_status[code] = "ACTIVE"
        else:
            elapsed = now - last_change_ts.get(code, now)
            contract_status[code] = "STALE" if elapsed > timeout else "ACTIVE"
        prev_values[code] = cur

        row_type = str(row.get("type", "")).upper()
        if row_type == "ETF":
            underlying = str(row.get("underlying", "") or "").strip()
            if not underlying:
                underlying = code.replace(".SH", "").replace(".XSHG", "")
            last = row.get("last")
            if isinstance(last, (int, float)) and last > 0:
                etf_prices[underlying] = float(last)

    # 品种熔断：核心平值合约（若可识别）或退化为全量合约
    groups: Dict[str, list[Dict[str, Any]]] = {}
    for row in rows:
        if str(row.get("type", "")).upper() == "ETF":
            continue
        underlying = str(row.get("underlying", "") or "").strip()
        if underlying:
            groups.setdefault(underlying, []).append(row)

    for underlying, opts in groups.items():
        etf_price = float(etf_prices.get(underlying, 0.0) or 0.0)
        watched = []
        if etf_price > 0:
            for row in opts:
                strike = row.get("strike")
                try:
                    if strike is None or str(strike).strip() == "":
                        continue
                    strike_v = float(str(strike).upper().rstrip("A"))
                except ValueError:
                    continue
                if strike_v > 0 and abs(strike_v - etf_price) / etf_price <= 0.05:
                    watched.append(row)
        if not watched:
            watched = opts

        stale_count = 0
        for row in watched:
            code = str(row.get("code", "") or "").strip()
            if contract_status.get(code) == "STALE":
                stale_count += 1
        product_fused[underlying] = stale_count > 0

    for row in rows:
        code = str(row.get("code", "") or "").strip()
        underlying = str(row.get("underlying", "") or "").strip()
        row["health"] = contract_status.get(code, "ACTIVE")
        row["fused"] = bool(product_fused.get(underlying, False)) if underlying else False

    fused_underlyings = sorted([k for k, v in product_fused.items() if v])
    return {
        "timeout": timeout,
        "fused_underlyings": fused_underlyings,
        "fused_count": len(fused_underlyings),
        "stale_count": sum(1 for v in contract_status.values() if v == "STALE"),
        "active_count": sum(1 for v in contract_status.values() if v == "ACTIVE"),
    }


class MonitorStartRequest(BaseModel):
    min_profit: float = 30.0
    expiry_days: int = 90
    refresh: int = 3
    n_each_side: int = 10
    zmq_port: int = 5555
    snapshot_dir: str = DEFAULT_MARKET_DATA_DIR
    etf_fee_rate: float = 0.0002
    option_one_side_fee: float = 1.5


class DDEStartRequest(BaseModel):
    interval: float = 3.0


class RecorderStartRequest(BaseModel):
    source: str = Field(default="wind", pattern="^(wind|dde)$")
    zmq_port: int = 5555


class DDEPipelineStartRequest(BaseModel):
    zmq_port: int = 5555
    refresh: int = 3
    min_profit: float = 30.0
    expiry_days: int = 90
    n_each_side: int = 10


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return TEMPLATE_PATH.read_text(encoding="utf-8")


@app.get("/dde", response_class=HTMLResponse)
def dde_page() -> str:
    return DDE_TEMPLATE_PATH.read_text(encoding="utf-8")


def _default_dde_excel_files() -> Dict[str, str]:
    candidates = {
        "510050.SH": "metadata/wxy_50etf.xlsx",
        "510300.SH": "metadata/wxy_300etf.xlsx",
        "510500.SH": "metadata/wxy_500etf.xlsx",
    }
    out: Dict[str, str] = {}
    for code, rel in candidates.items():
        if Path(rel).exists():
            out[code] = rel
    return out


def _require_dde_running():
    global _dde_feeder
    if _dde_feeder is None:
        raise HTTPException(status_code=409, detail="DDE 采集未启动，请先点击“启动 DDE”")
    return _dde_feeder


def _get_running_recorder_source() -> Optional[str]:
    recs = find_recorder_processes()
    if not recs:
        return None
    cmd = safe_cmdline(recs[0])
    return arg_from_cmd(cmd, "--source", "wind").lower() or "wind"


def _ensure_infinitrader_running() -> None:
    """
    启动 DDE 相关流程前，先确认交易终端已启动，避免 DataBus 启动后立即退出。
    """
    procs = find_infinitrader_processes()
    if not procs:
        raise HTTPException(
            status_code=409,
            detail="未检测到 InfiniTrader 进程，请先打开交易软件后再启动 DDE DataBus",
        )


def _get_cached_dde_routes() -> Dict[str, Any]:
    """
    懒解析 DDE 路由（用于在 recorder 模式补齐 strike/type/underlying 信息）。
    """
    excel_files = _default_dde_excel_files()
    if not excel_files:
        return {}
    key_parts = []
    for _, rel in sorted(excel_files.items()):
        p = Path(rel)
        if p.exists():
            key_parts.append(f"{rel}:{p.stat().st_mtime}")
    key = "|".join(key_parts)
    if _dde_routes_cache.get("key") == key:
        return _dde_routes_cache.get("routes", {})

    try:
        from data_engine.dde_adapter import DDERouteParser

        parser = DDERouteParser(excel_files)
        parser.parse()
        routes = parser.routes
    except Exception:
        routes = {}
    _dde_routes_cache["key"] = key
    _dde_routes_cache["routes"] = routes
    return routes


def _poll_from_recorder_snapshot(wind_info: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """
    recorder 运行时，从 snapshot_latest.parquet 读取展示数据。
    """
    try:
        import pandas as pd
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"读取 recorder 快照失败（缺少 pandas）: {exc}")

    snap = Path(DEFAULT_MARKET_DATA_DIR) / "snapshot_latest.parquet"
    if not snap.exists():
        return {
            "ok": True,
            "time": bj_now_naive().strftime("%Y-%m-%d %H:%M:%S"),
            "route_count": 0,
            "rows": [],
            "valid_last_count": 0,
            "mode": "databus",
        }

    try:
        df = pd.read_parquet(str(snap))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"读取 snapshot_latest.parquet 失败: {exc}")

    routes = _get_cached_dde_routes()
    rows = []
    valid_last = 0
    for _, rec in df.iterrows():
        code = str(rec.get("code", "") or "").strip()
        if not code:
            continue
        last = rec.get("last")
        if isinstance(last, (int, float)):
            valid_last += 1

        route = routes.get(code) or routes.get(code.replace(".SH", "").replace(".XSHG", ""))
        row_type = str(rec.get("type", "") or "").upper()
        is_etf = row_type == "ETF" or (route is not None and route.option_type == "ETF")
        strike_val = "" if is_etf else (route.strike if route else "")
        underlying = ""
        if route is not None:
            underlying = (route.underlying or "").replace(".SH", "").replace(".XSHG", "")
        if not underlying:
            underlying = str(rec.get("underlying", "") or "").replace(".SH", "").replace(".XSHG", "")
        if is_etf and not underlying:
            underlying = code.replace(".SH", "").replace(".XSHG", "")

        expiry_str = ""
        if not is_etf:
            code_bare = code.replace(".SH", "").replace(".XSHG", "")
            info = wind_info.get(code_bare)
            if info and info.get("expiry_date"):
                expiry_str = info["expiry_date"].strftime("%Y-%m-%d")

        rows.append(
            {
                "code": code,
                "underlying": underlying,
                "type": ("ETF" if is_etf else (route.option_type if route else "OPTION")),
                "strike": strike_val,
                "expiry": expiry_str,
                "last": rec.get("last"),
                "bid1": rec.get("bid1"),
                "ask1": rec.get("ask1"),
                # snapshot 不包含一档委托量，留空
                "bidv1": None,
                "askv1": None,
            }
        )

    rows.sort(key=lambda r: (r["underlying"], r["type"], r["code"]))
    health = _update_dde_health_from_rows(rows)
    return {
        "ok": True,
        "time": bj_now_naive().strftime("%Y-%m-%d %H:%M:%S"),
        "route_count": len(rows),
        "rows": rows,
        "valid_last_count": valid_last,
        "mode": "databus",
        "health": health,
    }


@app.post("/api/dde/start")
def start_dde(req: DDEStartRequest) -> Dict[str, Any]:
    global _dde_feeder
    _ensure_infinitrader_running()
    with _dde_lock:
        if _dde_feeder is not None:
            raise HTTPException(status_code=409, detail="DDE 采集已在运行")

        excel_files = _default_dde_excel_files()
        if not excel_files:
            raise HTTPException(status_code=400, detail="未找到 DDE 映射文件（metadata/wxy_*.xlsx）")

        try:
            from data_engine.dde_adapter import DDEDataFeeder
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"导入 DDE 模块失败: {exc}")

        feeder = DDEDataFeeder(excel_files=excel_files, poll_interval=req.interval)
        ok = feeder.start()
        if not ok:
            feeder.stop()
            raise HTTPException(status_code=500, detail="DDE 启动失败，请检查交易软件/DDE 配置")
        _dde_feeder = feeder
        return {
            "ok": True,
            "interval": req.interval,
            "route_count": len(feeder.routes),
            "excel_files": excel_files,
        }


@app.post("/api/dde/stop")
def stop_dde() -> Dict[str, Any]:
    global _dde_feeder
    with _dde_lock:
        if _dde_feeder is None:
            return {"ok": True, "running": False}
        try:
            _dde_feeder.stop()
        finally:
            _dde_feeder = None
    return {"ok": True, "running": False}


@app.get("/api/dde/state")
def dde_state() -> Dict[str, Any]:
    global _dde_feeder
    running = _dde_feeder is not None
    route_count = len(_dde_feeder.routes) if running else 0
    recorder_source = _get_running_recorder_source()
    return {
        "running": running,
        "route_count": route_count,
        "optionchain_mtime": _wind_optionchain_mtime_ago(),
        "wxy_50etf_mtime": _file_mtime_ago(_METADATA_DIR / "wxy_50etf.xlsx"),
        "wxy_300etf_mtime": _file_mtime_ago(_METADATA_DIR / "wxy_300etf.xlsx"),
        "wxy_500etf_mtime": _file_mtime_ago(_METADATA_DIR / "wxy_500etf.xlsx"),
        "data_mode": "databus" if recorder_source else "direct_dde",
        "recorder_source": recorder_source,
    }


@app.get("/api/dde/poll")
def dde_poll() -> Dict[str, Any]:
    wind_info = _load_wind_optionchain()
    recorder_source = _get_running_recorder_source()
    if recorder_source:
        return _poll_from_recorder_snapshot(wind_info)

    if _dde_feeder is None:
        return {
            "ok": True,
            "time": bj_now_naive().strftime("%Y-%m-%d %H:%M:%S"),
            "route_count": 0,
            "rows": [],
            "valid_last_count": 0,
            "health": {
                "timeout": float(_dde_health_cache.get("timeout", 30.0) or 30.0),
                "fused_underlyings": [],
                "fused_count": 0,
                "stale_count": 0,
                "active_count": 0,
            },
        }

    feeder = _dde_feeder
    today = bj_today()
    with _dde_lock:
        data = feeder.poll_once()
        rows = []
        valid_last = 0
        for code in sorted(data.keys()):
            quote = data[code]
            route = feeder.routes.get(code)
            last = quote.get("LASTPRICE")
            if isinstance(last, (float, int)):
                valid_last += 1
            is_etf = route and route.option_type == "ETF"
            strike_val = "" if is_etf else (route.strike if route else "")
            expiry_str = ""
            underlying = (route.underlying if route else "").replace(".SH", "").replace(".XSHG", "").strip()
            if not is_etf:
                code_bare = code.replace(".SH", "").replace(".XSHG", "").strip()
                info = wind_info.get(code_bare)
                if info and info.get("expiry_date"):
                    expiry_str = info["expiry_date"].strftime("%Y-%m-%d")
            rows.append(
                {
                    "code": code,
                    "underlying": underlying,
                    "type": route.option_type if route else "",
                    "strike": strike_val,
                    "expiry": expiry_str,
                    "last": quote.get("LASTPRICE"),
                    "bid1": quote.get("BIDPRICE1"),
                    "ask1": quote.get("ASKPRICE1"),
                    "bidv1": quote.get("BIDVOLUME1"),
                    "askv1": quote.get("ASKVOLUME1"),
                }
            )
        health = _update_dde_health_from_rows(rows)
        return {
            "ok": True,
            "time": bj_now_naive().strftime("%Y-%m-%d %H:%M:%S"),
            "route_count": len(feeder.routes),
            "rows": rows,
            "valid_last_count": valid_last,
            "health": health,
        }


@app.get("/api/dde/diag")
def dde_diag(code: Optional[str] = None, limit: int = 8) -> Dict[str, Any]:
    """
    DDE 诊断接口：返回原始值(raw) + 解析值(parsed) + 错误信息(error)。
    - code: 指定单个合约代码（可选）
    - limit: 未指定 code 时，最多诊断前 N 条合约
    """
    feeder = _require_dde_running()
    with _dde_lock:
        request_fields = feeder.client.request_fields
        selected_codes: list[str]
        if code:
            if code not in feeder.routes:
                raise HTTPException(status_code=404, detail=f"未找到合约: {code}")
            selected_codes = [code]
        else:
            selected_codes = sorted(feeder.routes.keys())[: max(1, min(limit, 50))]

        rows = []
        total = 0
        parsed_count = 0
        error_count = 0
        for c in selected_codes:
            route = feeder.routes[c]
            field_diag: Dict[str, Dict[str, Any]] = {}
            for field in request_fields:
                parsed, raw, err, ords = feeder.client.request_diagnostic(route.topic, field)
                total += 1
                if err:
                    error_count += 1
                if parsed is not None:
                    parsed_count += 1
                field_diag[field] = {
                    "parsed": parsed,
                    "raw_hex": raw,
                    "error": err,
                    "raw_len": len(ords),
                }
            rows.append(
                {
                    "code": c,
                    "type": route.option_type,
                    "topic": route.topic,
                    "source": route.source_file,
                    "fields": field_diag,
                }
            )

        return {
            "ok": True,
            "time": bj_now_naive().strftime("%Y-%m-%d %H:%M:%S"),
            "route_count": len(feeder.routes),
            "selected_count": len(selected_codes),
            "stats": {
                "total_fields": total,
                "parsed_fields": parsed_count,
                "error_fields": error_count,
            },
            "rows": rows,
        }


@app.get("/api/state")
def get_state() -> Dict[str, Any]:
    rec_procs = find_recorder_processes()
    mon_procs = find_monitor_processes()
    all_procs = [process_info(p, "databus") for p in rec_procs] + [process_info(p, "monitor") for p in mon_procs]
    snapshot_raw = read_snapshot_stats(DEFAULT_MARKET_DATA_DIR)
    chunks_raw = count_today_chunks(DEFAULT_MARKET_DATA_DIR)
    merge_status = merge_status_readable(DEFAULT_MARKET_DATA_DIR, bj_today())
    return {
        "server_time": bj_now_naive().strftime("%Y-%m-%d %H:%M:%S"),
        "processes": all_procs,
        "recorder_running": len(rec_procs) > 0,
        "recorder_count": len(rec_procs),
        "monitor_count": len(mon_procs),
        "snapshot": snapshot_readable(snapshot_raw),
        "chunks": chunks_readable(chunks_raw),
        "merge_status": merge_status,
        "market_data_dir": DEFAULT_MARKET_DATA_DIR,
        "metadata_files": {
            "wind_sse_optionchain": {"mtime_ago": _wind_optionchain_mtime_ago()},
            "wxy_50etf":  {"mtime_ago": _file_mtime_ago(_METADATA_DIR / "wxy_50etf.xlsx")},
            "wxy_300etf": {"mtime_ago": _file_mtime_ago(_METADATA_DIR / "wxy_300etf.xlsx")},
            "wxy_500etf": {"mtime_ago": _file_mtime_ago(_METADATA_DIR / "wxy_500etf.xlsx")},
        },
        "bond_files": _bond_files_info(),
    }


@app.post("/api/processes/recorder/start")
def start_recorder(req: RecorderStartRequest) -> Dict[str, Any]:
    existing = find_recorder_processes()
    if existing:
        raise HTTPException(status_code=409, detail=f"DataBus 已在运行 (PID {existing[0].pid})，请先关闭")
    if req.source == "dde":
        _ensure_infinitrader_running()
    args = ["--source", req.source, "--port", str(req.zmq_port)]
    return {"ok": True, "started": spawn_module("data_bus.bus", args)}


@app.post("/api/processes/monitor/start")
def start_monitor(req: MonitorStartRequest) -> Dict[str, Any]:
    args = [
        "--min-profit", str(req.min_profit),
        "--expiry-days", str(req.expiry_days),
        "--refresh", str(req.refresh),
        "--n-each-side", str(req.n_each_side),
        "--zmq-port", str(req.zmq_port),
        "--snapshot-dir", req.snapshot_dir,
        "--etf-fee-rate", str(req.etf_fee_rate),
        "--option-one-side-fee", str(req.option_one_side_fee),
    ]
    return {"ok": True, "started": spawn_module("monitors.monitor", args)}


@app.post("/api/pipelines/dde/start")
def start_dde_pipeline(req: DDEPipelineStartRequest) -> Dict[str, Any]:
    """
    一键启动 DDE 链路：
    DDE DataBus (--source dde) -> Monitor
    """
    existing_rec = find_recorder_processes()
    existing_mon = find_monitor_processes()
    if existing_rec:
        raise HTTPException(status_code=409, detail=f"DataBus 已在运行 (PID {existing_rec[0].pid})，请先关闭")
    if existing_mon:
        raise HTTPException(status_code=409, detail=f"Monitor 已在运行 (PID {existing_mon[0].pid})，请先关闭")
    _ensure_infinitrader_running()

    rec_started = spawn_module("data_bus.bus", ["--source", "dde", "--port", str(req.zmq_port)])
    # 给 recorder 一点启动时间，避免 monitor 过早连接
    time.sleep(0.8)
    mon_args = [
        "--zmq-port", str(req.zmq_port),
        "--snapshot-dir", DEFAULT_MARKET_DATA_DIR,
        "--min-profit", str(req.min_profit),
        "--expiry-days", str(req.expiry_days),
        "--refresh", str(req.refresh),
        "--n-each-side", str(req.n_each_side),
    ]
    mon_started = spawn_module("monitors.monitor", mon_args)
    return {
        "ok": True,
        "pipeline": "dde->databus->monitor",
        "recorder": rec_started,
        "monitor": mon_started,
        "zmq_port": req.zmq_port,
    }


@app.post("/api/processes/{pid}/kill")
def kill_process(pid: int) -> Dict[str, Any]:
    try:
        proc = psutil.Process(pid)
    except psutil.NoSuchProcess:
        raise HTTPException(status_code=404, detail="进程不存在")
    try:
        proc.terminate()
        proc.wait(timeout=5)
        return {"ok": True, "pid": pid, "forced": False}
    except psutil.TimeoutExpired:
        proc.kill()
        return {"ok": True, "pid": pid, "forced": True}
    except psutil.AccessDenied:
        raise HTTPException(status_code=403, detail="权限不足")


@app.post("/api/processes/{pid}/reopen")
def reopen_process(pid: int) -> Dict[str, Any]:
    try:
        proc = psutil.Process(pid)
    except psutil.NoSuchProcess:
        raise HTTPException(status_code=404, detail="进程不存在")

    cmd = safe_cmdline(proc)
    if _is_real_databus_proc(proc):
        module = "data_bus.bus"
        args = ["--source", arg_from_cmd(cmd, "--source", "wind")]
        if "--no-persist" in cmd:
            args.append("--no-persist")
    elif _is_real_monitor_proc(proc):
        module = "monitors.monitor"
        args = [
            "--min-profit",
            arg_from_cmd(cmd, "--min-profit", "30"),
            "--expiry-days",
            arg_from_cmd(cmd, "--expiry-days", "90"),
            "--refresh",
            arg_from_cmd(cmd, "--refresh", "3"),
            "--n-each-side",
            arg_from_cmd(cmd, "--n-each-side", "10"),
            "--zmq-port",
            arg_from_cmd(cmd, "--zmq-port", "5555"),
            "--snapshot-dir",
            arg_from_cmd(cmd, "--snapshot-dir", DEFAULT_MARKET_DATA_DIR),
        ]
    else:
        raise HTTPException(status_code=400, detail="无法识别进程类型")

    try:
        proc.terminate()
        proc.wait(timeout=5)
    except psutil.TimeoutExpired:
        proc.kill()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        pass

    started = spawn_module(module, args)
    return {"ok": True, "old_pid": pid, "started": started}


@app.post("/api/processes/kill-all")
def kill_all() -> Dict[str, Any]:
    killed = []
    for proc in find_recorder_processes() + find_monitor_processes():
        try:
            proc.terminate()
            proc.wait(timeout=5)
            killed.append(proc.pid)
        except psutil.TimeoutExpired:
            proc.kill()
            killed.append(proc.pid)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return {"ok": True, "killed": killed}


@app.post("/api/actions/fetch-optionchain")
def start_fetch_optionchain() -> Dict[str, Any]:
    today_str = bj_today().strftime("%Y-%m-%d")
    if not launch_fetch_task(today_str):
        raise HTTPException(status_code=409, detail="期权链抓取正在进行中")
    return {"ok": True, "date": today_str}


@app.get("/api/actions/fetch-status")
def fetch_status() -> Dict[str, Any]:
    return get_fetch_state()


@app.post("/api/actions/merge")
def run_merge_api() -> Dict[str, Any]:
    return run_merge(bj_today(), DEFAULT_MARKET_DATA_DIR)


class FetchBondRequest(BaseModel):
    kind: str = "all"  # shibor | cgb | all


@app.post("/api/actions/fetch-bond")
def fetch_bond(req: FetchBondRequest) -> Dict[str, Any]:
    """抓取当日 CGB 和/或 Shibor 收益率曲线，保存至 D:\\MARKET_DATA\\macro。"""
    from data_engine.bond_termstructure_fetcher import (
        save_shibor_daily,
        save_cgb_yieldcurve_daily,
    )
    target_date = bj_today()
    base_dir = Path(DEFAULT_MARKET_DATA_DIR)
    output: list[str] = []
    ok = True
    try:
        if req.kind in ("shibor", "all"):
            path = save_shibor_daily(target_date, base_dir=base_dir)
            output.append(f"Shibor 已保存: {path.name}")
        if req.kind in ("cgb", "all"):
            path = save_cgb_yieldcurve_daily(target_date, base_dir=base_dir)
            output.append(f"CGB 已保存: {path.name}")
    except Exception as e:
        ok = False
        output.append(str(e))
    return {
        "ok": ok,
        "date": target_date.strftime("%Y-%m-%d"),
        "kind": req.kind,
        "output": "\n".join(output),
        "bond_files": _bond_files_info(),
    }


def main() -> None:
    import argparse
    import uvicorn

    parser = argparse.ArgumentParser(description="DeltaZero Web 控制台")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址")
    parser.add_argument("--port", type=int, default=8787, help="监听端口")
    parser.add_argument("--reload", action="store_true", help="开发模式热重载")
    args = parser.parse_args()
    uvicorn.run("web.dashboard:app", host=args.host, port=args.port, reload=args.reload)


if __name__ == "__main__":
    main()
