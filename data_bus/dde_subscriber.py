# -*- coding: utf-8 -*-
"""
DDE 实时行情订阅器（轮询模式）。

职责：
1. 复用 DDERouteParser + DDEClientManager 建立 DDE 路由和连接
2. 按固定间隔轮询盘口字段
3. 全天轮询并将 tick 封装为 TickPacket 放入队列（是否落盘由 DataBus 决定）
4. 复用 Recorder 主循环，进入 ParquetWriter + ZMQPublisher 管线
"""

from __future__ import annotations

import glob
import logging
import math
import re
import threading
import time
import zipfile
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta
from pathlib import Path
from queue import Queue
from typing import Any, Dict, List, Optional

from data_engine.dde_adapter import DDERouteParser, DDEClientManager, RouteEntry
from models import DataProvider, ETFTickData, TickData, TickPacket, normalize_code
from utils.time_utils import bj_now_naive

logger = logging.getLogger(__name__)

_NS_MAIN = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
_COL_RE = re.compile(r"([A-Z]+)")
_WIND_CHAIN_GLOB = "metadata/wind_sse_optionchain.xlsx"

_HEALTH_ACTIVE = "ACTIVE"
_HEALTH_STALE = "STALE"
_CORE_FIELDS = ("LASTPRICE", "BIDPRICE1", "ASKPRICE1")


class DDESubscriber(DataProvider):
    """
    DDE 轮询订阅器。
    对外接口与 WindSubscriber 尽量保持一致：start/stop/option_count。
    """

    def __init__(
        self,
        products: List[str],
        tick_queue: Queue,
        poll_interval: float = 3.0,
        staleness_timeout: float = 90.0,
        mode: str = "advise",
    ) -> None:
        self._products = list(products)
        self._queue = tick_queue
        self._poll_interval = max(0.5, float(poll_interval))
        self._staleness_timeout = max(1.0, float(staleness_timeout))
        self._mode = mode if mode in ("advise", "request") else "advise"
        self._is_running = False
        self._thread: Optional[threading.Thread] = None

        self._routes: Dict[str, RouteEntry] = {}
        self._client: Optional[DDEClientManager] = None
        self._startup_done = threading.Event()
        self._startup_ok = False
        self._startup_error = ""

        # option_code(.SH) -> underlying(.SH)
        self._code_to_underlying: Dict[str, str] = {}
        self._code_multiplier: Dict[str, int] = {}
        self._code_is_adjusted: set[str] = set()

        # Health check state
        self._prev_values: Dict[str, Dict[str, Optional[float]]] = {}
        self._last_change_ts: Dict[str, float] = {}
        self._contract_status: Dict[str, str] = {}
        self._product_fused: Dict[str, bool] = {}
        self._etf_prices: Dict[str, float] = {}
        self._health_lock = threading.Lock()

    @property
    def option_count(self) -> int:
        return len(self._code_to_underlying)

    @property
    def etf_count(self) -> int:
        return len([r for r in self._routes.values() if r.option_type == "ETF"])

    @property
    def active_underlyings(self) -> List[str]:
        codes = {normalize_code(r.underlying, ".SH") for r in self._routes.values() if r.underlying}
        return sorted(codes)

    def start(self) -> bool:
        excel_files = self._build_default_dde_excel_files(self._products)
        if not excel_files:
            logger.error("未找到 DDE 映射文件（metadata/wxy_*.xlsx）")
            return False

        parser = DDERouteParser(excel_files, logger=logger)
        parser.parse()
        self._routes = parser.routes
        if not self._routes:
            logger.error("DDE 路由解析为空，启动失败")
            return False

        # 加载 multiplier/is_adjusted 信息（缺失则 fallback）
        self._load_optionchain_info()
        self._build_code_maps_from_routes()

        now = time.time()
        with self._health_lock:
            for code in self._routes:
                self._last_change_ts.setdefault(code, now)
                self._contract_status.setdefault(code, _HEALTH_ACTIVE)
            for entry in self._routes.values():
                key = self._underlying_key(entry)
                if key:
                    self._product_fused.setdefault(key, False)

        self._is_running = True
        target = self._advise_loop if self._mode == "advise" else self._poll_loop
        self._thread = threading.Thread(target=target, name="dde-subscriber", daemon=True)
        self._thread.start()
        # 等待轮询线程完成 DDE 初始化（DDE 连接需与请求在同线程）
        self._startup_done.wait(timeout=10.0)
        if not self._startup_ok:
            self._is_running = False
            if self._thread and self._thread.is_alive():
                self._thread.join(timeout=2.0)
            self._thread = None
            if self._startup_error:
                logger.error("DDE 订阅启动失败: %s", self._startup_error)
            else:
                logger.error("DDE 订阅启动失败: 初始化超时")
            return False

        logger.info(
            "DDE 订阅启动成功 [%s]: %d 路由（连接 %d/%d），期权 %d，ETF %d",
            self._mode.upper(),
            len(self._routes),
            getattr(self, "_connected_ok", 0),
            getattr(self, "_connected_total", 0),
            self.option_count,
            self.etf_count,
        )
        return True

    def stop(self) -> None:
        self._is_running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3.0)
        self._thread = None
        if self._client:
            self._client.close()
            self._client = None
        logger.info("DDE 订阅已停止")

    def is_trading_safe(self, underlying: str) -> bool:
        key = normalize_code((underlying or "").strip(), ".SH")
        if not key:
            return True
        with self._health_lock:
            return not self._product_fused.get(key, False)

    def get_health_report(self) -> Dict[str, Any]:
        with self._health_lock:
            now = time.time()
            stale_seconds = {
                code: max(0.0, now - self._last_change_ts.get(code, now))
                for code in self._contract_status
            }
            return {
                "contract_status": dict(self._contract_status),
                "product_fused": dict(self._product_fused),
                "etf_prices": dict(self._etf_prices),
                "stale_seconds": stale_seconds,
                "timeout": self._staleness_timeout,
            }

    def _underlying_key(self, entry: RouteEntry, raw_code: str = "") -> str:
        if entry.option_type == "ETF":
            return normalize_code(entry.contract_code, ".SH")
        if entry.underlying:
            return normalize_code(entry.underlying, ".SH")
        if raw_code:
            return self._code_to_underlying.get(normalize_code(raw_code, ".SH"), "")
        return ""

    def _snapshot_fused_map(self) -> Dict[str, bool]:
        with self._health_lock:
            return dict(self._product_fused)

    def _update_staleness(self, data: Dict[str, Dict[str, Optional[float]]]) -> None:
        now = time.time()
        with self._health_lock:
            for code, row in data.items():
                prev = self._prev_values.get(code, {})
                changed = any(row.get(field) != prev.get(field) for field in _CORE_FIELDS)
                if changed:
                    self._last_change_ts[code] = now
                    self._contract_status[code] = _HEALTH_ACTIVE
                else:
                    last_ts = self._last_change_ts.get(code, now)
                    elapsed = now - last_ts
                    if elapsed > self._staleness_timeout:
                        if self._contract_status.get(code) != _HEALTH_STALE:
                            logger.warning("合约 %s 数据陈旧 %.1fs，标记 STALE", code, elapsed)
                        self._contract_status[code] = _HEALTH_STALE
                    else:
                        self._contract_status[code] = _HEALTH_ACTIVE
                self._prev_values[code] = dict(row)

            for code, entry in self._routes.items():
                if entry.option_type != "ETF":
                    continue
                last = _f(data.get(code, {}).get("LASTPRICE"))
                if _is_valid_price(last):
                    key = self._underlying_key(entry, code)
                    if key:
                        self._etf_prices[key] = last

            self._evaluate_circuit_breakers_locked()

    def _evaluate_circuit_breakers_locked(self) -> None:
        groups: Dict[str, List[str]] = {}
        for code, entry in self._routes.items():
            if entry.option_type == "ETF":
                continue
            key = self._underlying_key(entry, code)
            if not key:
                continue
            groups.setdefault(key, []).append(code)

        for underlying, codes in groups.items():
            etf_price = self._etf_prices.get(underlying, 0.0)
            atm_codes = self._find_atm_contracts(codes, etf_price)
            watched_codes = atm_codes if atm_codes else codes
            stale_count = sum(1 for c in watched_codes if self._contract_status.get(c) == _HEALTH_STALE)

            was_fused = self._product_fused.get(underlying, False)
            if stale_count > 0:
                if not was_fused:
                    logger.critical(
                        "[ALERT] %s 期权数据流中断或 UI 未激活，已触发熔断保护！（%d/%d 核心合约 STALE）",
                        underlying,
                        stale_count,
                        len(watched_codes),
                    )
                self._product_fused[underlying] = True
            else:
                if was_fused:
                    logger.info("[RECOVERED] %s 数据流已恢复，解除熔断", underlying)
                self._product_fused[underlying] = False

    def _find_atm_contracts(self, codes: List[str], etf_price: float) -> List[str]:
        if etf_price <= 0:
            return []

        out: List[str] = []
        for code in codes:
            entry = self._routes.get(code)
            if not entry:
                continue
            strike_text = (entry.strike or "").strip().upper()
            if not strike_text:
                continue
            try:
                strike = float(strike_text.rstrip("A"))
            except ValueError:
                continue
            if strike <= 0:
                continue
            if abs(strike - etf_price) / etf_price <= 0.05:
                out.append(code)
        return out

    def _poll_loop(self) -> None:
        try:
            self._client = DDEClientManager(logger=logger)
            if not self._client.initialized:
                self._startup_error = "DDE 客户端初始化失败"
                self._startup_ok = False
                self._startup_done.set()
                return
            ok, total = self._client.connect_routes(self._routes)
            self._connected_ok = ok
            self._connected_total = total
            if ok <= 0:
                self._startup_error = f"DDE 连接失败: {ok}/{total}"
                self._startup_ok = False
                self._startup_done.set()
                return
            self._startup_ok = True
            self._startup_done.set()
        except Exception as exc:
            self._startup_error = str(exc)
            self._startup_ok = False
            self._startup_done.set()
            return

        while self._is_running:
            data = self._client.poll_data(self._routes)
            self._update_staleness(data)

            ts = bj_now_naive()
            ts_ms = int(ts.timestamp() * 1000)

            # 熔断时仍向队列/ZMQ 发送 tick，保证 Monitor 有数据可展示；交易侧用 is_trading_safe() 阻断
            for raw_code, row in data.items():
                entry = self._routes.get(raw_code)
                if not entry:
                    continue
                if entry.option_type == "ETF":
                    self._emit_etf_tick(entry, row, ts, ts_ms)
                else:
                    self._emit_option_tick(raw_code, entry, row, ts, ts_ms)

            time.sleep(self._poll_interval)

    def _advise_loop(self) -> None:
        """ADVISE 热链接模式：由交易软件推送驱动，无固定轮询间隔。"""
        # --- DDE 初始化（与 _poll_loop 相同）---
        try:
            self._client = DDEClientManager(logger=logger)
            if not self._client.initialized:
                self._startup_error = "DDE 客户端初始化失败"
                self._startup_ok = False
                self._startup_done.set()
                return
            ok, total = self._client.connect_routes(self._routes)
            self._connected_ok = ok
            self._connected_total = total
            if ok <= 0:
                self._startup_error = f"DDE 连接失败: {ok}/{total}"
                self._startup_ok = False
                self._startup_done.set()
                return
        except Exception as exc:
            self._startup_error = str(exc)
            self._startup_ok = False
            self._startup_done.set()
            return

        # --- 注册 ADVISE 热链接 ---
        adv_ok, adv_total = self._client.advise_start_all(self._routes)
        if adv_ok <= 0:
            logger.warning(
                "ADVISE 注册全部失败 (%d/%d)，回退到 REQUEST 轮询模式",
                adv_ok, adv_total,
            )
            self._mode = "request"
            self._startup_ok = True
            self._startup_done.set()
            # 回退到 poll 循环
            while self._is_running:
                data = self._client.poll_data(self._routes)
                self._update_staleness(data)
                ts = bj_now_naive()
                ts_ms = int(ts.timestamp() * 1000)
                for raw_code, row in data.items():
                    entry = self._routes.get(raw_code)
                    if not entry:
                        continue
                    if entry.option_type == "ETF":
                        self._emit_etf_tick(entry, row, ts, ts_ms)
                    else:
                        self._emit_option_tick(raw_code, entry, row, ts, ts_ms)
                time.sleep(self._poll_interval)
            return

        logger.info("ADVISE 模式就绪: %d/%d 热链接注册成功", adv_ok, adv_total)
        self._startup_ok = True
        self._startup_done.set()

        # --- 事件驱动主循环 ---
        last_staleness_check = time.time()
        while self._is_running:
            dirty = self._client.pump_and_collect()

            if dirty:
                ts = bj_now_naive()
                ts_ms = int(ts.timestamp() * 1000)
                for topic, fields in dirty.items():
                    codes = self._client._topic_to_codes.get(topic, [])
                    for raw_code in codes:
                        entry = self._routes.get(raw_code)
                        if not entry:
                            continue
                        if entry.option_type == "ETF":
                            self._emit_etf_tick(entry, fields, ts, ts_ms)
                        else:
                            self._emit_option_tick(raw_code, entry, fields, ts, ts_ms)

            # 定期 staleness 检查（每秒一次）
            now = time.time()
            if now - last_staleness_check >= 1.0:
                full = self._client.get_full_snapshot()
                # 将 topic-keyed 快照转为 code-keyed 格式（与 _update_staleness 兼容）
                code_data: Dict[str, Dict[str, Optional[float]]] = {}
                for topic, snapshot in full.items():
                    for raw_code in self._client._topic_to_codes.get(topic, []):
                        code_data[raw_code] = snapshot
                self._update_staleness(code_data)
                last_staleness_check = now

            time.sleep(0.005)  # 5ms，避免 CPU 空转

    def _emit_option_tick(
        self,
        raw_code: str,
        entry: RouteEntry,
        row: Dict[str, Optional[float]],
        ts: datetime,
        ts_ms: int,
    ) -> None:
        code = normalize_code(raw_code, ".SH")
        underlying = normalize_code(entry.underlying or "", ".SH")
        if not underlying:
            return

        last = _f(row.get("LASTPRICE"))
        ask1 = _f(row.get("ASKPRICE1"))
        bid1 = _f(row.get("BIDPRICE1"))
        askv1 = _i(row.get("ASKVOLUME1"))
        bidv1 = _i(row.get("BIDVOLUME1"))

        # 非交易时段 DDE 常出现 bid/ask 空值；只要 last 有效就继续下发，
        # 并用 last 回退一档价，保证 Monitor 仍可见数据流。
        if not _is_valid_price(last):
            return
        if not _is_valid_price(ask1):
            ask1 = last
        if not _is_valid_price(bid1):
            bid1 = last

        multiplier = self._code_multiplier.get(code, 10000)
        is_adjusted = code in self._code_is_adjusted

        tick_row = {
            "ts": ts_ms,
            "code": code,
            "underlying": underlying,
            "last": last,
            "ask1": ask1,
            "bid1": bid1,
            "askv1": askv1,
            "bidv1": bidv1,
            "oi": 0,
            "vol": 0,
            "high": last,
            "low": last,
            "is_adjusted": is_adjusted,
            "multiplier": multiplier,
        }

        tick_obj = TickData(
            timestamp=ts,
            contract_code=code,
            current=last,
            volume=0,
            high=last,
            low=last,
            money=0.0,
            position=0,
            ask_prices=[ask1] + [math.nan] * 4,
            ask_volumes=[askv1] + [0] * 4,
            bid_prices=[bid1] + [math.nan] * 4,
            bid_volumes=[bidv1] + [0] * 4,
        )

        pkt = TickPacket(
            is_etf=False,
            tick_row=tick_row,
            tick_obj=tick_obj,
            underlying_code=underlying,
        )
        try:
            self._queue.put_nowait(pkt)
        except Exception:
            pass

    def _emit_etf_tick(
        self,
        entry: RouteEntry,
        row: Dict[str, Optional[float]],
        ts: datetime,
        ts_ms: int,
    ) -> None:
        code = normalize_code(entry.contract_code, ".SH")
        last = _f(row.get("LASTPRICE"))
        ask1 = _f(row.get("ASKPRICE1"))
        bid1 = _f(row.get("BIDPRICE1"))
        askv1 = _i(row.get("ASKVOLUME1"))
        bidv1 = _i(row.get("BIDVOLUME1"))
        if not _is_valid_price(last):
            return

        tick_row = {
            "ts": ts_ms,
            "code": code,
            "last": last,
            "ask1": ask1,
            "bid1": bid1,
            "askv1": askv1,
            "bidv1": bidv1,
        }
        tick_obj = ETFTickData(
            timestamp=ts,
            etf_code=code,
            price=last,
            ask_price=ask1,
            bid_price=bid1,
            ask_volume=askv1,
            bid_volume=bidv1,
            is_simulated=False,
        )
        pkt = TickPacket(is_etf=True, tick_row=tick_row, tick_obj=tick_obj, underlying_code=code)
        try:
            self._queue.put_nowait(pkt)
        except Exception:
            pass

    def _build_code_maps_from_routes(self) -> None:
        for code, entry in self._routes.items():
            if entry.option_type == "ETF":
                continue
            norm_code = normalize_code(code, ".SH")
            underlying = normalize_code(entry.underlying or "", ".SH")
            if not underlying:
                continue
            self._code_to_underlying[norm_code] = underlying
            # fallback: strike 带 A 或合约代码尾字母（少见）视为调整型
            strike = (entry.strike or "").upper()
            if strike.endswith("A"):
                self._code_is_adjusted.add(norm_code)
            self._code_multiplier.setdefault(norm_code, 10000)

    def _load_optionchain_info(self) -> None:
        """从 wind_sse_optionchain.xlsx 补充 is_adjusted/multiplier。"""
        files = sorted(glob.glob(_WIND_CHAIN_GLOB))
        if not files:
            return
        for fpath in files:
            try:
                with zipfile.ZipFile(fpath, "r") as zf:
                    self._load_single_wind_xlsx(zf)
            except Exception as exc:
                logger.debug("读取 %s 失败: %s", fpath, exc)

    def _load_single_wind_xlsx(self, zf: zipfile.ZipFile) -> None:
        shared: List[str] = []
        if "xl/sharedStrings.xml" in zf.namelist():
            try:
                root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
                for si in root.findall(".//s:si", _NS_MAIN):
                    parts = [t.text or "" for t in si.findall(".//s:t", _NS_MAIN)]
                    shared.append("".join(parts))
            except Exception:
                pass

        def _cell_val(cell: ET.Element) -> str:
            t_attr = (cell.get("t") or "").strip()
            v_elem = cell.find("s:v", _NS_MAIN)
            val = v_elem.text if v_elem is not None and v_elem.text is not None else ""
            if t_attr == "s" and val:
                try:
                    return shared[int(val)]
                except Exception:
                    return ""
            return str(val)

        for name in zf.namelist():
            if not name.startswith("xl/worksheets/sheet") or not name.endswith(".xml"):
                continue
            try:
                root = ET.fromstring(zf.read(name))
            except Exception:
                continue
            rows = root.findall(".//s:sheetData/s:row", _NS_MAIN)
            for row in rows[1:]:
                cell_map: Dict[str, str] = {}
                for cell in row.findall("s:c", _NS_MAIN):
                    ref = cell.get("r", "")
                    m = _COL_RE.match(ref)
                    if not m:
                        continue
                    col = m.group(1)
                    cell_map[col] = _cell_val(cell).strip()

                code_raw = (cell_map.get("A") or "").strip()
                if not code_raw or not code_raw[0].isdigit():
                    continue
                code = normalize_code(code_raw, ".SH")
                if not code.split(".")[0].isdigit():
                    continue
                if len(code.split(".")[0]) < 8:
                    # ETF 行
                    continue

                short_name = (cell_map.get("B") or "").strip().upper()
                strike = (cell_map.get("E") or "").strip().upper()
                mult_raw = (cell_map.get("I") or "").strip()
                try:
                    mult = int(float(mult_raw)) if mult_raw else 10000
                except Exception:
                    mult = 10000
                self._code_multiplier[code] = mult if mult > 0 else 10000
                if short_name.endswith("A") or strike.endswith("A") or self._code_multiplier[code] != 10000:
                    self._code_is_adjusted.add(code)

    @staticmethod
    def _build_default_dde_excel_files(products: List[str]) -> Dict[str, str]:
        candidate_map = {
            "510050.SH": "metadata/wxy_50etf.xlsx",
            "510300.SH": "metadata/wxy_300etf.xlsx",
            "510500.SH": "metadata/wxy_500etf.xlsx",
        }
        out: Dict[str, str] = {}
        for p in products:
            rel = candidate_map.get(p)
            if rel and Path(rel).exists():
                out[p] = rel
        return out


def _f(v: Optional[float]) -> float:
    try:
        return float(v) if v is not None else math.nan
    except Exception:
        return math.nan


def _i(v: Optional[float]) -> int:
    try:
        return int(round(float(v))) if v is not None else 0
    except Exception:
        return 0


def _is_valid_price(v: float) -> bool:
    return not math.isnan(v) and v > 0

