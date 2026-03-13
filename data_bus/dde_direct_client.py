# -*- coding: utf-8 -*-
"""data_bus/dde_direct_client.py — ctypes DDEML DDE 直连行情（ADVISE 模式）。

service / topic 全部来自 wxy_options.xlsx 路由表，每次 DataBus 启动时读取一次。
对外接口：DDEDirectSubscriber（与 WindSubscriber 兼容）。
"""
from __future__ import annotations

import glob
import logging
import math
import re
import struct
import threading
import time
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from queue import Queue
from typing import Callable, Dict, Iterable, List, Optional, Tuple

from models import DataProvider, ETFTickData, TickData, TickPacket, normalize_code

logger = logging.getLogger(__name__)

# ── QD DDE 字段映射（英文 item 名，来自 wxy_*.xlsx ddeItems 实测）────────────
_FIELD_MAP: Dict[str, str] = {
    "last":  "LASTPRICE",
    "bid1":  "BIDPRICE1",
    "ask1":  "ASKPRICE1",
    "bidv1": "BIDVOLUME1",
    "askv1": "ASKVOLUME1",
}


# ── xlsx 路由解析常量 ────────────────────────────────────────────────────────
_NS_MAIN = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
_NS_REL  = {"r": "http://schemas.openxmlformats.org/package/2006/relationships"}
_COL_RE  = re.compile(r"([A-Z]+)")
_LINK_RE = re.compile(r"\[(\d+)\]")
_EXTERNAL_LINK_RE = re.compile(r"externalLink(\d+)\.xml$")

_CALL_CONTRACT_COL  = "K"
_PUT_CONTRACT_COL   = "M"
_STRIKE_COL         = "L"
_CALL_FORMULA_COLS  = ("D", "A", "I")
_PUT_FORMULA_COLS   = ("T", "O", "W")
_ETF_CODE_COL       = "A"
_ETF_FORMULA_COLS   = ("H", "I", "D")

_WIND_CHAIN_GLOB    = "metadata/wind_sse_optionchain.xlsx"

# ── 辅助函数 ────────────────────────────────────────────────────────────────

def _f(v) -> float:
    try:
        return float(v) if v is not None else math.nan
    except Exception:
        return math.nan


def _i(v) -> int:
    try:
        return int(round(float(v))) if v is not None else 0
    except Exception:
        return 0


def _is_valid_price(v: float) -> bool:
    return not math.isnan(v) and v > 0


# ── xlsx 路由解析（原 DDERouteParser 逻辑，重构为模块级私有函数）─────────────

def _xls_read_workbook_sheets(zf: zipfile.ZipFile) -> List[Tuple[str, str]]:
    workbook_file = "xl/workbook.xml"
    rels_file = "xl/_rels/workbook.xml.rels"
    if workbook_file not in zf.namelist() or rels_file not in zf.namelist():
        return [("sheet1", "xl/worksheets/sheet1.xml")]

    wb_root  = ET.fromstring(zf.read(workbook_file))
    rel_root = ET.fromstring(zf.read(rels_file))

    rel_map: Dict[str, str] = {}
    for rel in rel_root.findall(".//r:Relationship", _NS_REL):
        rid    = (rel.attrib.get("Id") or "").strip()
        target = (rel.attrib.get("Target") or "").strip()
        if rid and target:
            rel_map[rid] = target

    out: List[Tuple[str, str]] = []
    for sheet in wb_root.findall(".//s:sheets/s:sheet", _NS_MAIN):
        name = (sheet.attrib.get("name") or "").strip()
        rid  = sheet.attrib.get(
            "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id", ""
        )
        target = rel_map.get(rid, "")
        if not target or not target.startswith("worksheets/"):
            continue
        path = f"xl/{target}"
        if path in zf.namelist():
            out.append((name or Path(path).stem, path))

    return out or [("sheet1", "xl/worksheets/sheet1.xml")]


def _xls_read_shared_strings(zf: zipfile.ZipFile) -> List[str]:
    if "xl/sharedStrings.xml" not in zf.namelist():
        return []
    try:
        root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
    except Exception:
        return []
    values: List[str] = []
    for si in root.findall(".//s:si", _NS_MAIN):
        parts = [t.text or "" for t in si.findall(".//s:t", _NS_MAIN)]
        values.append("".join(parts))
    return values


def _xls_read_external_links_full(zf: zipfile.ZipFile) -> Dict[int, Tuple[str, str]]:
    """返回 {index: (service, topic)}，service 和 topic 均保留。"""
    out: Dict[int, Tuple[str, str]] = {}
    for name in zf.namelist():
        if not name.startswith("xl/externalLinks/externalLink") or not name.endswith(".xml"):
            continue
        match = _EXTERNAL_LINK_RE.search(name)
        if not match:
            continue
        idx = int(match.group(1))
        try:
            root = ET.fromstring(zf.read(name))
        except Exception:
            continue
        dde_link = root.find(".//s:ddeLink", _NS_MAIN)
        if dde_link is None:
            continue
        server = (dde_link.get("ddeService") or "").strip()
        topic  = (dde_link.get("ddeTopic") or "").strip()
        if server and topic:
            out[idx] = (server, topic)
    return out


def _xls_row_to_cell_map(
    row_elem: ET.Element, shared_strings: List[str]
) -> Dict[str, Dict[str, str]]:
    out: Dict[str, Dict[str, str]] = {}
    for cell in row_elem.findall("s:c", _NS_MAIN):
        ref = cell.get("r", "")
        col_match = _COL_RE.match(ref)
        if not col_match:
            continue
        col    = col_match.group(1)
        t_attr = (cell.get("t") or "").strip()
        v_elem = cell.find("s:v", _NS_MAIN)
        f_elem = cell.find("s:f", _NS_MAIN)
        val     = v_elem.text if v_elem is not None and v_elem.text is not None else ""
        formula = f_elem.text if f_elem is not None and f_elem.text is not None else ""
        if t_attr == "s" and val:
            try:
                idx = int(val)
                val = shared_strings[idx] if 0 <= idx < len(shared_strings) else ""
            except ValueError:
                val = ""
        out[col] = {"val": str(val).strip(), "formula": str(formula).strip()}
    return out


def _xls_safe_text(cell_map: Dict[str, Dict[str, str]], col: str) -> str:
    return (cell_map.get(col, {}).get("val") or "").strip()


def _xls_resolve_server_topic(
    cell_map: Dict[str, Dict[str, str]],
    link_map: Dict[int, Tuple[str, str]],
    formula_cols: Iterable[str],
) -> Tuple[str, str]:
    for col in formula_cols:
        formula = (cell_map.get(col, {}).get("formula") or "").strip()
        if not formula:
            continue
        match = _LINK_RE.search(formula)
        if not match:
            continue
        idx = int(match.group(1))
        if idx in link_map:
            return link_map[idx]
    return "", ""


def _xls_parse_sheet(
    zf: zipfile.ZipFile,
    shared_strings: List[str],
    link_map: Dict[int, Tuple[str, str]],
    sheet_path: str,
    sheet_name: str,
    source_name: str,
    underlying: str = "",
) -> Dict[str, str]:
    """Return {contract_code: topic} for one sheet."""
    if sheet_path not in zf.namelist():
        return {}

    root = ET.fromstring(zf.read(sheet_path))
    rows = root.findall(".//s:sheetData/s:row", _NS_MAIN)
    result: Dict[str, str] = {}

    # 若未传入 underlying，预扫描 ETF 行自动检测本 sheet 的标的代码
    sheet_underlying = underlying
    if not sheet_underlying:
        for row in rows[1:]:
            cell_map = _xls_row_to_cell_map(row, shared_strings)
            etf_code_raw = _xls_safe_text(cell_map, _ETF_CODE_COL)
            if etf_code_raw and etf_code_raw.isdigit():
                sheet_underlying = _normalize_underlying_code(etf_code_raw)
                break

    for row in rows[1:]:
        cell_map = _xls_row_to_cell_map(row, shared_strings)
        if not cell_map:
            continue

        call_code = _xls_safe_text(cell_map, _CALL_CONTRACT_COL)
        put_code  = _xls_safe_text(cell_map, _PUT_CONTRACT_COL)

        is_option_row = _is_option_contract_code(call_code) or _is_option_contract_code(put_code)
        if is_option_row:
            call_server, call_topic = _xls_resolve_server_topic(cell_map, link_map, _CALL_FORMULA_COLS)
            put_server,  put_topic  = _xls_resolve_server_topic(cell_map, link_map, _PUT_FORMULA_COLS)
            if call_code and call_server and call_topic:
                result[call_code] = call_topic
            if put_code and put_server and put_topic:
                result[put_code] = put_topic
            continue

        # ETF 行
        etf_code_raw = _xls_safe_text(cell_map, _ETF_CODE_COL)
        if not etf_code_raw or not etf_code_raw.isdigit():
            continue
        etf_server, etf_topic = _xls_resolve_server_topic(cell_map, link_map, _ETF_FORMULA_COLS)
        if etf_server and etf_topic:
            etf_code = _normalize_underlying_code(etf_code_raw)
            result[etf_code] = etf_topic

    return result


def _normalize_underlying_code(code: str) -> str:
    c = code.strip()
    return c if "." in c else f"{c}.SH"


def _is_option_contract_code(code: str) -> bool:
    c = (code or "").strip()
    return c.isdigit() and len(c) >= 8


def _parse_xlsx_topic_map(xlsx_path: Path, underlying: str = "") -> Tuple[Dict[str, str], str]:
    """
    Parse one wxy_*.xlsx.
    Returns ({code: topic}, service_name).
    service_name 从 externalLink 读取（如 'QD'），找不到时返回空字符串。
    """
    result: Dict[str, str] = {}
    service_name: str = ""
    try:
        with zipfile.ZipFile(xlsx_path, "r") as zf:
            shared_strings = _xls_read_shared_strings(zf)
            link_map_full  = _xls_read_external_links_full(zf)   # {idx: (service, topic)}
            if not link_map_full:
                logger.warning("未发现 externalLink，文件可能不是 DDE 路由表: %s", xlsx_path.name)
            # 提取 service 名（取第一个非空）
            for svc, _ in link_map_full.values():
                if svc:
                    service_name = svc
                    break
            sheet_defs = _xls_read_workbook_sheets(zf)
            for sheet_name, sheet_path in sheet_defs:
                sheet_result = _xls_parse_sheet(
                    zf, shared_strings, link_map_full,  # 传完整 (service, topic) 元组
                    sheet_path, sheet_name, xlsx_path.name, underlying,
                )
                if sheet_result:
                    logger.info("路由解析: %s / %s → %d 条", xlsx_path.name, sheet_name, len(sheet_result))
                result.update(sheet_result)
    except Exception as exc:
        logger.warning("解析 %s 失败: %s", xlsx_path, exc)
    return result, service_name


def _load_topic_map(
    products: List[str],
    metadata_dir: Optional[Path] = None,
) -> Tuple[Dict[str, str], str]:
    """
    从 wxy_*.xlsx 文件解析 topic 路由表。

    Returns:
        ({内部合约代码: DDE topic}, service_name)
    """
    base = metadata_dir or Path("metadata")
    candidate_map = {
        "510050.SH": base / "wxy_50etf.xlsx",
        "510300.SH": base / "wxy_300etf.xlsx",
        "510500.SH": base / "wxy_500etf.xlsx",
    }

    # 优先合并文件
    consolidated = base / "wxy_options.xlsx"
    if consolidated.exists():
        result, service = _parse_xlsx_topic_map(consolidated)
        logger.info("_load_topic_map: 合并文件 %d 条路由, service=%r", len(result), service)
        return result, service

    merged: Dict[str, str] = {}
    service: str = ""
    for product in products:
        path = candidate_map.get(product)
        if path and path.exists():
            entries, svc = _parse_xlsx_topic_map(path, underlying=product)
            merged.update(entries)
            if svc and not service:
                service = svc
        elif product in candidate_map:
            logger.warning("路由文件不存在，跳过品种 %s: %s", product, candidate_map[product])

    logger.info("_load_topic_map: 共 %d 条路由（%d 个品种）, service=%r", len(merged), len(products), service)
    return merged, service


# ── ctypes DDEML 常量与类型（替代 pywin32 dde，已验证可用）─────────────────
import ctypes
import ctypes.wintypes as _wt

_CF_TEXT           = 1
_XTYP_ADVSTART     = 0x1030
_XTYP_ADVDATA      = 0x4010
_DDE_FACK          = 0x8000
_APPCMD_CLIENTONLY = 0x00000010
_CP_WINUNICODE     = 1200
_DDE_TIMEOUT_MS    = 5000

_ITEM_TO_FIELD: Dict[str, str] = {v: k for k, v in _FIELD_MAP.items()}

class _MSG(ctypes.Structure):
    _fields_ = [
        ("hwnd",    _wt.HWND),
        ("message", _wt.UINT),
        ("wParam",  _wt.WPARAM),
        ("lParam",  _wt.LPARAM),
        ("time",    _wt.DWORD),
        ("pt",      _wt.POINT),
    ]

_DDECALLBACK = ctypes.WINFUNCTYPE(
    ctypes.c_void_p,
    _wt.UINT, _wt.UINT,
    ctypes.c_void_p, ctypes.c_void_p,
    ctypes.c_void_p, ctypes.c_void_p,
    ctypes.POINTER(_wt.ULONG),
    ctypes.POINTER(_wt.ULONG),
)


def _setup_ddeml():
    u32 = ctypes.windll.user32
    u32.DdeInitializeW.argtypes   = [ctypes.POINTER(_wt.DWORD), _DDECALLBACK, _wt.DWORD, _wt.DWORD]
    u32.DdeInitializeW.restype    = _wt.UINT
    u32.DdeCreateStringHandleW.argtypes = [_wt.DWORD, _wt.LPCWSTR, ctypes.c_int]
    u32.DdeCreateStringHandleW.restype  = ctypes.c_void_p
    u32.DdeFreeStringHandle.argtypes    = [_wt.DWORD, ctypes.c_void_p]
    u32.DdeFreeStringHandle.restype     = _wt.BOOL
    u32.DdeConnect.argtypes       = [_wt.DWORD, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
    u32.DdeConnect.restype        = ctypes.c_void_p
    u32.DdeDisconnect.argtypes    = [ctypes.c_void_p]
    u32.DdeDisconnect.restype     = _wt.BOOL
    u32.DdeClientTransaction.argtypes = [
        ctypes.c_void_p, _wt.DWORD, ctypes.c_void_p, ctypes.c_void_p,
        _wt.UINT, _wt.UINT, _wt.DWORD, ctypes.POINTER(_wt.DWORD),
    ]
    u32.DdeClientTransaction.restype  = ctypes.c_void_p
    u32.DdeAccessData.argtypes    = [ctypes.c_void_p, ctypes.POINTER(_wt.DWORD)]
    u32.DdeAccessData.restype     = ctypes.c_void_p
    u32.DdeUnaccessData.argtypes  = [ctypes.c_void_p]
    u32.DdeUnaccessData.restype   = _wt.BOOL
    u32.DdeFreeDataHandle.argtypes= [ctypes.c_void_p]
    u32.DdeFreeDataHandle.restype = _wt.BOOL
    u32.DdeUninitialize.argtypes  = [_wt.DWORD]
    u32.DdeUninitialize.restype   = _wt.BOOL
    u32.DdeGetLastError.argtypes  = [_wt.DWORD]
    u32.DdeGetLastError.restype   = _wt.UINT
    u32.DdeQueryStringW.argtypes  = [_wt.DWORD, ctypes.c_void_p, ctypes.c_wchar_p, _wt.DWORD, ctypes.c_int]
    u32.DdeQueryStringW.restype   = _wt.DWORD
    u32.PeekMessageW.argtypes     = [ctypes.POINTER(_MSG), _wt.HWND, _wt.UINT, _wt.UINT, _wt.UINT]
    u32.PeekMessageW.restype      = _wt.BOOL
    u32.TranslateMessage.argtypes = [ctypes.POINTER(_MSG)]
    u32.TranslateMessage.restype  = _wt.BOOL
    u32.DispatchMessageW.argtypes = [ctypes.POINTER(_MSG)]
    u32.DispatchMessageW.restype  = ctypes.c_long
    return u32


def _dde_parse_response(raw: bytes) -> Optional[float]:
    """解析 DDE 响应字节（XlTable 二进制 或 文本）。"""
    if not raw:
        return None
    # XlTable 二进制：从 off=0 流式读 (type, size) 记录对
    # TABLE(0x0010) 是外层容器，rsize 字节后才是真正的数据记录
    _XLTBL_FLOAT = 0x0001
    _XLTBL_INT   = 0x0006
    _XLTBL_BLANK = 0x0005
    _XLTBL_ERROR = 0x0004
    _XLTBL_TABLE = 0x0010
    if len(raw) >= 4 and struct.unpack_from("<H", raw, 0)[0] in (
        _XLTBL_TABLE, _XLTBL_FLOAT, _XLTBL_INT, _XLTBL_BLANK, _XLTBL_ERROR
    ):
        off = 0
        while off + 4 <= len(raw):
            rtype = struct.unpack_from("<H", raw, off)[0]
            rsize = struct.unpack_from("<H", raw, off + 2)[0]
            off += 4
            if rtype == _XLTBL_FLOAT and rsize == 8 and off + 8 <= len(raw):
                return struct.unpack_from("<d", raw, off)[0]
            if rtype == _XLTBL_INT and rsize == 2 and off + 2 <= len(raw):
                return float(struct.unpack_from("<h", raw, off)[0])
            if rtype in (_XLTBL_BLANK, _XLTBL_ERROR):
                return None
            off += rsize
        return None
    # 文本格式兜底
    txt = raw.rstrip(b"\x00").decode("gbk", errors="ignore").strip()
    txt = txt.replace(",", "").replace("%", "")
    if not txt or txt in ("--", "N/A", "None", "nan"):
        return None
    try:
        return float(txt)
    except ValueError:
        return None


# ── _DDEClient（ctypes DDEML ADVISE 模式，内部实现）─────────────────────────

class _DDEClient:
    """
    ctypes DDEML 直连通达信行情（ADVISE 模式）。

    on_tick 签名：(code: str, fields: dict, ts_ms: int) -> None
    fields 包含 {"last": float, "bid1": float, ...}
    """

    def __init__(
        self,
        on_tick: Callable,
        service: str = "QD",
        heartbeat_interval: float = 30.0,
        retry_interval: float = 10.0,
    ) -> None:
        self._on_tick_cb    = on_tick
        self._service       = service
        self._hb_interval   = heartbeat_interval
        self._retry_interval = retry_interval
        self._running       = False
        self._pump_thread: Optional[threading.Thread] = None

        # 由 start() 填充
        self._topic_to_code: Dict[str, str] = {}   # DDE topic → internal code

        # tick 缓冲（code → field → value），攒够核心字段后回调
        self._tick_buf: Dict[str, Dict[str, float]] = {}
        self._buf_lock = threading.Lock()
        self._cb_total  = 0   # ADVISE 回调总次数（含未攒够的）
        self._tick_total = 0  # 成功 emit 的 tick 数

        # ctypes DDEML 状态（在泵送线程中初始化）
        self._u32         = None
        self._idInst      = _wt.DWORD(0)
        self._callback_ref= None   # 防止 GC 回收
        self._convs: Dict[str, ctypes.c_void_p] = {}  # topic → hConv

    # ── 公开接口 ──────────────────────────────────────────

    def start(
        self,
        codes: list[str],
        topic_map: Optional[Dict[str, str]] = None,
        service: str = "QD",
    ) -> None:
        self._service = service
        topic_map = topic_map or {}
        # 构建 topic → code 反向映射；topic 来自 xlsx，无则跳过
        for code in codes:
            raw = topic_map.get(code, "")
            if not raw:
                continue
            self._topic_to_code[raw] = code

        self._running = True
        self._pump_thread = threading.Thread(
            target=self._message_loop,
            daemon=True,
            name="dde-direct-pump",
        )
        self._pump_thread.start()
        logger.info("_DDEClient: 已启动，订阅 %d 个合约", len(codes))

    def stop(self) -> None:
        self._running = False
        if self._pump_thread:
            self._pump_thread.join(timeout=5.0)
        logger.info("_DDEClient: 已停止")

    # ── 内部实现（ctypes DDEML）──────────────────────────

    def _message_loop(self) -> None:
        """Windows 消息泵线程：初始化 DDEML → 订阅 ADVISE → 消息泵。"""
        u32 = _setup_ddeml()
        self._u32 = u32

        self._callback_ref = _DDECALLBACK(self._dde_callback)
        ret = u32.DdeInitializeW(
            ctypes.byref(self._idInst),
            self._callback_ref,
            _APPCMD_CLIENTONLY,
            0,
        )
        if ret != 0:
            logger.error("DdeInitializeW 失败, ret=%d", ret)
            return

        logger.info("DDEML 初始化成功, idInst=%d", self._idInst.value)
        try:
            self._connect_and_advise()

            # 全部连接失败时按间隔重试，直到有连接成功或程序停止
            retry_attempt = 1
            msg = _MSG()
            while self._running and not self._convs:
                logger.info(
                    "DDE 全部连接失败，%ds 后第 %d 次重试（客户端可能未就绪）...",
                    int(self._retry_interval), retry_attempt,
                )
                deadline = time.time() + self._retry_interval
                while self._running and time.time() < deadline:
                    while u32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
                        u32.TranslateMessage(ctypes.byref(msg))
                        u32.DispatchMessageW(ctypes.byref(msg))
                    time.sleep(0.1)
                if self._running:
                    retry_attempt += 1
                    self._connect_and_advise()

            last_hb = time.time()

            while self._running:
                # 泵送 Windows 消息（ADVISE 回调在此触发）
                while u32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
                    u32.TranslateMessage(ctypes.byref(msg))
                    u32.DispatchMessageW(ctypes.byref(msg))
                time.sleep(0.005)

                if time.time() - last_hb > self._hb_interval:
                    last_hb = time.time()
                    logger.info("DDE 心跳: %d 个活跃连接, 累计回调 %d 次, 累计 tick %d 条",
                                len(self._convs), self._cb_total, self._tick_total)

        except Exception as e:
            logger.error("DDE message loop 异常: %s", e)
        finally:
            self._close_ddeml()

    def _make_hsz(self, text: str) -> ctypes.c_void_p:
        return self._u32.DdeCreateStringHandleW(self._idInst, text, _CP_WINUNICODE)

    def _free_hsz(self, hsz) -> None:
        if hsz:
            self._u32.DdeFreeStringHandle(self._idInst, hsz)

    def _query_string(self, hsz) -> str:
        buf = ctypes.create_unicode_buffer(256)
        n   = self._u32.DdeQueryStringW(self._idInst, hsz, buf, 256, _CP_WINUNICODE)
        return buf.value if n > 0 else ""

    def _pump_messages(self) -> None:
        """泵送所有待处理 Windows 消息（DDE 握手需要）。"""
        msg = _MSG()
        while self._u32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
            self._u32.TranslateMessage(ctypes.byref(msg))
            self._u32.DispatchMessageW(ctypes.byref(msg))

    def _connect_and_advise(self) -> None:
        """为所有 topic 建立 DDE 连接并注册 ADVISE。"""
        u32 = self._u32
        # 初始化线程消息队列（DDE 握手依赖 PeekMessage）
        self._pump_messages()
        srv_hsz = self._make_hsz(self._service)
        ok = fail = 0
        try:
            for topic, code in self._topic_to_code.items():
                top_hsz = self._make_hsz(topic)
                try:
                    hConv = u32.DdeConnect(self._idInst, srv_hsz, top_hsz, None)
                    self._pump_messages()   # 泵送 WM_DDE_ACK，旧版 connect_topic() 同样做法
                    if not hConv:
                        err = u32.DdeGetLastError(self._idInst)
                        logger.warning("DDE 连接失败 %s (topic=%s) err=0x%04x", code, topic, err)
                        fail += 1
                        continue
                    self._convs[topic] = hConv
                    # 为每个字段注册 ADVISE
                    for item_name in _FIELD_MAP.values():
                        item_hsz = self._make_hsz(item_name)
                        try:
                            u32.DdeClientTransaction(
                                None, 0, hConv, item_hsz, _CF_TEXT,
                                _XTYP_ADVSTART, _DDE_TIMEOUT_MS, None,
                            )
                        finally:
                            self._free_hsz(item_hsz)
                    ok += 1
                finally:
                    self._free_hsz(top_hsz)
        finally:
            self._free_hsz(srv_hsz)
        logger.info("DDE 连接: %d 成功 / %d 失败（共 %d 合约）", ok, fail, len(self._topic_to_code))

    def _dde_callback(self, uType, uFmt, hconv, hsz1, hsz2, hdata, dw1, dw2):
        """DDEML 回调（在消息泵线程中执行，不能阻塞）。"""
        if uType == _XTYP_ADVDATA:
            try:
                topic = self._query_string(hsz1)
                item  = self._query_string(hsz2)
                field = _ITEM_TO_FIELD.get(item)
                code  = self._topic_to_code.get(topic)
                if field and code:
                    value = self._read_advise_data(hdata)
                    if value is not None:
                        if "v" in field:  # bidv1 / askv1
                            value = float(int(round(value)))
                        self._accumulate(code, field, value)
            except Exception:
                pass
            return _DDE_FACK
        return 0

    def _read_advise_data(self, hdata) -> Optional[float]:
        if not hdata:
            return None
        cb   = _wt.DWORD(0)
        pData = self._u32.DdeAccessData(hdata, ctypes.byref(cb))
        if not pData or cb.value == 0:
            return None
        try:
            raw = ctypes.string_at(pData, cb.value)
        finally:
            self._u32.DdeUnaccessData(hdata)
        return _dde_parse_response(raw)

    def _accumulate(self, code: str, field: str, value: float) -> None:
        ts_ms = int(time.time() * 1000)
        with self._buf_lock:
            self._cb_total += 1
            if self._cb_total <= 5:
                logger.info("DDE 回调 #%d: code=%s field=%s value=%s", self._cb_total, code, field, value)
            buf = self._tick_buf.setdefault(code, {})
            buf[field] = value
            if len(buf) >= 3:
                self._on_tick_cb(code, dict(buf), ts_ms)
                self._tick_total += 1
                if self._tick_total <= 3:
                    logger.info("DDE tick #%d: code=%s fields=%s", self._tick_total, code, list(buf.keys()))
                buf.clear()

    def _close_ddeml(self) -> None:
        u32 = self._u32
        if u32 is None:
            return
        for hConv in self._convs.values():
            try:
                u32.DdeDisconnect(hConv)
            except Exception:
                pass
        self._convs.clear()
        if self._idInst.value:
            u32.DdeUninitialize(self._idInst)
            self._idInst = _wt.DWORD(0)


# ── DDEDirectSubscriber ────────────────────────────────────────────────────────

class DDEDirectSubscriber(DataProvider):
    """
    DDE 直连订阅器（ctypes DDEML ADVISE 模式）。

    接口与 WindSubscriber 兼容：start/stop/option_count/etf_count/active_underlyings。
    """

    def __init__(
        self,
        products: List[str],
        tick_queue: Queue,
        metadata_dir: Optional[Path] = None,
    ) -> None:
        self._products = list(products)
        self._queue    = tick_queue
        self._metadata_dir = metadata_dir

        self._client = _DDEClient(on_tick=self._on_tick)

        # 由 start() 填充
        self._etf_codes: set = set()
        self._code_to_underlying: Dict[str, str] = {}
        self._code_multiplier: Dict[str, int] = {}
        self._code_is_adjusted: set = set()
        self._option_count: int = 0
        self._etf_count: int = 0
        self._active_underlyings: List[str] = []
        self._unknown_codes: set = set()  # 已警告过的未知合约，避免重复刷屏

    @property
    def option_count(self) -> int:
        return self._option_count

    @property
    def etf_count(self) -> int:
        return self._etf_count

    @property
    def active_underlyings(self) -> List[str]:
        return self._active_underlyings

    def start(self) -> bool:
        # 1. 加载 topic_map（wxy_*.xlsx）
        topic_map, service = _load_topic_map(self._products, self._metadata_dir)
        if not topic_map:
            logger.error("DDEDirectSubscriber: topic_map 为空，请检查 metadata/wxy_*.xlsx")
            return False

        # 2. 加载合约元数据（multiplier / is_adjusted / underlying）
        self._load_contract_metadata(topic_map)

        # 3. 订阅码列表 = topic_map 中所有代码（含 ETF）
        all_codes = list(topic_map.keys())
        self._option_count = len(self._code_to_underlying)
        self._etf_count    = len(self._etf_codes)
        self._active_underlyings = sorted(set(self._code_to_underlying.values()))

        # 4. 启动 DDE 客户端（service/topic 全部来自 xlsx）
        self._client.start(codes=all_codes, topic_map=topic_map, service=service or "QD")
        logger.info(
            "DDEDirectSubscriber 启动: 期权 %d 个, ETF %d 个, 标的 %s",
            self._option_count,
            self._etf_count,
            self._active_underlyings or "N/A",
        )
        return True

    def stop(self) -> None:
        self._client.stop()
        logger.info("DDEDirectSubscriber: 已停止")

    def _on_tick(self, code: str, fields: dict, ts_ms: int) -> None:
        """DDE ADVISE 回调 → TickPacket → queue"""
        ts = datetime.fromtimestamp(ts_ms / 1000.0)

        if code in self._etf_codes:
            self._emit_etf_tick(code, fields, ts, ts_ms)
        else:
            norm_code = normalize_code(code, ".SH")
            underlying = self._code_to_underlying.get(norm_code)
            if not underlying:
                if norm_code not in self._unknown_codes:
                    self._unknown_codes.add(norm_code)
                    logger.warning("期权合约 %s 无 underlying（optionchain 未命中），tick 将被持续丢弃（仅警告一次）", norm_code)
                return
            self._emit_option_tick(norm_code, underlying, fields, ts, ts_ms)

    def _emit_option_tick(
        self,
        code: str,
        underlying: str,
        fields: dict,
        ts: datetime,
        ts_ms: int,
    ) -> None:
        last  = _f(fields.get("last"))
        bid1  = _f(fields.get("bid1"))
        ask1  = _f(fields.get("ask1"))
        bidv1 = _i(fields.get("bidv1"))
        askv1 = _i(fields.get("askv1"))

        if not _is_valid_price(last):
            return
        if not _is_valid_price(ask1):
            ask1 = last
        if not _is_valid_price(bid1):
            bid1 = last

        multiplier  = self._code_multiplier.get(code, 10000)
        is_adjusted = code in self._code_is_adjusted

        tick_row = {
            "ts":          ts_ms,
            "code":        code,
            "underlying":  underlying,
            "last":        last,
            "ask1":        ask1,
            "bid1":        bid1,
            "askv1":       askv1,
            "bidv1":       bidv1,
            "oi":          0,
            "vol":         0,
            "high":        last,
            "low":         last,
            "is_adjusted": is_adjusted,
            "multiplier":  multiplier,
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
        code: str,
        fields: dict,
        ts: datetime,
        ts_ms: int,
    ) -> None:
        norm_code = normalize_code(code, ".SH")
        last  = _f(fields.get("last"))
        bid1  = _f(fields.get("bid1"))
        ask1  = _f(fields.get("ask1"))
        bidv1 = _i(fields.get("bidv1"))
        askv1 = _i(fields.get("askv1"))

        if not _is_valid_price(last):
            return

        tick_row = {
            "ts":    ts_ms,
            "code":  norm_code,
            "last":  last,
            "ask1":  ask1,
            "bid1":  bid1,
            "askv1": askv1,
            "bidv1": bidv1,
        }
        tick_obj = ETFTickData(
            timestamp=ts,
            etf_code=norm_code,
            price=last,
            ask_price=ask1,
            bid_price=bid1,
            ask_volume=askv1,
            bid_volume=bidv1,
            is_simulated=False,
        )
        pkt = TickPacket(is_etf=True, tick_row=tick_row, tick_obj=tick_obj, underlying_code=norm_code)
        try:
            self._queue.put_nowait(pkt)
        except Exception:
            pass

    def _load_contract_metadata(self, topic_map: Dict[str, str]) -> None:
        """
        从 topic_map 中识别 ETF 代码；从 wind_sse_optionchain.xlsx 补充
        underlying / multiplier / is_adjusted。
        未在 optionchain 中的期权合约仅靠 topic_map 推断 underlying（回退为空，tick 被丢弃）。
        """
        # ETF 代码判断：非 8 位纯数字视为 ETF
        for code in topic_map:
            norm = normalize_code(code, ".SH")
            sym  = norm.split(".")[0]
            if sym.isdigit() and len(sym) >= 8:
                pass  # 期权
            else:
                self._etf_codes.add(code)

        # 从 optionchain xlsx 补充 multiplier / is_adjusted / underlying
        chain_files = sorted(glob.glob(_WIND_CHAIN_GLOB))
        if chain_files:
            self._load_optionchain_xlsx(chain_files[-1])
        else:
            logger.warning("未找到 %s，期权 multiplier/underlying 使用默认值", _WIND_CHAIN_GLOB)

        # 对 topic_map 中的期权合约，若 optionchain 未命中则尝试从品种推断 underlying
        _CODE_TO_UNDERLYING_GUESS = {
            "510050.SH":  "510050.SH",
            "510300.SH":  "510300.SH",
            "510500.SH":  "510500.SH",
        }
        for code in topic_map:
            norm = normalize_code(code, ".SH")
            if code in self._etf_codes:
                continue
            if norm in self._code_to_underlying:
                continue
            # 无法从 optionchain 找到 underlying，跳过（tick 将被 _on_tick 丢弃）
            logger.debug("期权合约 %s 未在 optionchain 中找到，无 underlying，将被跳过", norm)

    def _load_optionchain_xlsx(self, fpath: str) -> None:
        """从 wind_sse_optionchain.xlsx 读取 underlying / multiplier / is_adjusted。"""
        _NS = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
        _col_re = re.compile(r"([A-Z]+)")

        try:
            with zipfile.ZipFile(fpath, "r") as zf:
                shared: List[str] = []
                if "xl/sharedStrings.xml" in zf.namelist():
                    try:
                        root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
                        for si in root.findall(".//s:si", _NS):
                            parts = [t.text or "" for t in si.findall(".//s:t", _NS)]
                            shared.append("".join(parts))
                    except Exception:
                        pass

                def _cell_val(cell: ET.Element) -> str:
                    t_attr = (cell.get("t") or "").strip()
                    v_elem = cell.find("s:v", _NS)
                    val    = v_elem.text if v_elem is not None and v_elem.text is not None else ""
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
                    for row in root.findall(".//s:sheetData/s:row", _NS)[1:]:
                        cm: Dict[str, str] = {}
                        for cell in row.findall("s:c", _NS):
                            ref = cell.get("r", "")
                            m   = _col_re.match(ref)
                            if m:
                                cm[m.group(1)] = _cell_val(cell).strip()

                        code_raw = (cm.get("A") or "").strip()
                        if not code_raw or not code_raw[0].isdigit():
                            continue
                        code = normalize_code(code_raw, ".SH")
                        sym  = code.split(".")[0]
                        if not sym.isdigit() or len(sym) < 8:
                            continue  # ETF 行

                        # underlying（Wind code → .SH）
                        us_code = (cm.get("C") or "").strip()
                        if us_code:
                            underlying = normalize_code(us_code, ".SH")
                            self._code_to_underlying[code] = underlying

                        # multiplier
                        mult_raw = (cm.get("I") or "").strip()
                        try:
                            mult = int(float(mult_raw)) if mult_raw else 10000
                        except Exception:
                            mult = 10000
                        self._code_multiplier[code] = mult if mult > 0 else 10000

                        # is_adjusted
                        short_name = (cm.get("B") or "").strip().upper()
                        strike     = (cm.get("E") or "").strip().upper()
                        if short_name.endswith("A") or strike.endswith("A") or self._code_multiplier[code] != 10000:
                            self._code_is_adjusted.add(code)

        except Exception as exc:
            logger.warning("读取 optionchain 文件失败: %s — %s", fpath, exc)


