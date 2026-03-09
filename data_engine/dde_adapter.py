from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wintypes
import logging
import re
import struct
import threading
import time
import zipfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

_NS_MAIN = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
_NS_REL = {"r": "http://schemas.openxmlformats.org/package/2006/relationships"}
_COL_RE = re.compile(r"([A-Z]+)")
_DEFAULT_STOP_FILE = Path(__file__).resolve().parent.parent / "metadata" / ".dde_feeder_stop"
_LINK_RE = re.compile(r"\[(\d+)\]")
_EXTERNAL_LINK_RE = re.compile(r"externalLink(\d+)\.xml$")

# ---------------------------------------------------------------------------
# DDEML constants & ctypes setup
# ---------------------------------------------------------------------------
_CF_TEXT = 1
_XTYP_REQUEST = 0x20B0
_XTYP_ADVSTART = 0x1030
_XTYP_ADVSTOP = 0x8040
_XTYP_ADVDATA = 0x4010
_DDE_FACK = 0x8000
_APPCMD_CLIENTONLY = 0x00000010
_CP_WINUNICODE = 1200
_DDE_TIMEOUT_MS = 10000

_XLTBL_FLOAT = 0x0001
_XLTBL_STRING = 0x0002
_XLTBL_BOOL = 0x0003
_XLTBL_ERROR = 0x0004
_XLTBL_BLANK = 0x0005
_XLTBL_INT = 0x0006
_XLTBL_SKIP = 0x0007
_XLTBL_TABLE = 0x0010


class _MSG(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("message", wintypes.UINT),
        ("wParam", wintypes.WPARAM),
        ("lParam", wintypes.LPARAM),
        ("time", wintypes.DWORD),
        ("pt", wintypes.POINT),
    ]


_DDECALLBACK = ctypes.WINFUNCTYPE(
    ctypes.c_void_p,
    wintypes.UINT,
    wintypes.UINT,
    ctypes.c_void_p,
    ctypes.c_void_p,
    ctypes.c_void_p,
    ctypes.c_void_p,
    ctypes.POINTER(wintypes.ULONG),
    ctypes.POINTER(wintypes.ULONG),
)


def _setup_ddeml():
    """Set up argtypes/restype for DDEML functions once."""
    u32 = ctypes.windll.user32
    u32.DdeInitializeW.argtypes = [
        ctypes.POINTER(wintypes.DWORD), _DDECALLBACK, wintypes.DWORD, wintypes.DWORD
    ]
    u32.DdeInitializeW.restype = wintypes.UINT
    u32.DdeCreateStringHandleW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, ctypes.c_int]
    u32.DdeCreateStringHandleW.restype = ctypes.c_void_p
    u32.DdeFreeStringHandle.argtypes = [wintypes.DWORD, ctypes.c_void_p]
    u32.DdeFreeStringHandle.restype = wintypes.BOOL
    u32.DdeConnect.argtypes = [wintypes.DWORD, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
    u32.DdeConnect.restype = ctypes.c_void_p
    u32.DdeDisconnect.argtypes = [ctypes.c_void_p]
    u32.DdeDisconnect.restype = wintypes.BOOL
    u32.DdeClientTransaction.argtypes = [
        ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p, ctypes.c_void_p,
        wintypes.UINT, wintypes.UINT, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
    ]
    u32.DdeClientTransaction.restype = ctypes.c_void_p
    u32.DdeAccessData.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD)]
    u32.DdeAccessData.restype = ctypes.c_void_p
    u32.DdeUnaccessData.argtypes = [ctypes.c_void_p]
    u32.DdeUnaccessData.restype = wintypes.BOOL
    u32.DdeFreeDataHandle.argtypes = [ctypes.c_void_p]
    u32.DdeFreeDataHandle.restype = wintypes.BOOL
    u32.DdeUninitialize.argtypes = [wintypes.DWORD]
    u32.DdeUninitialize.restype = wintypes.BOOL
    u32.DdeGetLastError.argtypes = [wintypes.DWORD]
    u32.DdeGetLastError.restype = wintypes.UINT
    u32.DdeQueryStringW.argtypes = [
        wintypes.DWORD, ctypes.c_void_p, ctypes.c_wchar_p, wintypes.DWORD, ctypes.c_int,
    ]
    u32.DdeQueryStringW.restype = wintypes.DWORD
    return u32


# ---------------------------------------------------------------------------
# XlTable binary parser
# ---------------------------------------------------------------------------
def _parse_xltable(data: bytes) -> Optional[float]:
    """Extract the first numeric value from XlTable binary data."""
    off = 0
    while off + 4 <= len(data):
        rtype = struct.unpack_from("<H", data, off)[0]
        rsize = struct.unpack_from("<H", data, off + 2)[0]
        off += 4
        if rtype == _XLTBL_FLOAT and rsize == 8 and off + 8 <= len(data):
            return struct.unpack_from("<d", data, off)[0]
        if rtype == _XLTBL_INT and rsize == 2 and off + 2 <= len(data):
            return float(struct.unpack_from("<h", data, off)[0])
        if rtype in (_XLTBL_BLANK, _XLTBL_ERROR):
            return None
        off += rsize
    return None


def _parse_dde_response(raw: bytes) -> Optional[float]:
    """
    Parse raw DDE response bytes: auto-detect XlTable binary vs plain text.
    """
    if len(raw) < 4:
        return _try_text_value(raw)
    first_type = struct.unpack_from("<H", raw, 0)[0]
    if first_type == _XLTBL_TABLE:
        return _parse_xltable(raw)
    return _try_text_value(raw)


def _try_text_value(raw: bytes) -> Optional[float]:
    txt = raw.rstrip(b"\x00").decode("gbk", errors="ignore").strip()
    if not txt or txt in ("--", "N/A", "None", "nan"):
        return None
    txt = txt.replace(",", "").replace("%", "")
    try:
        return float(txt)
    except ValueError:
        return None


@dataclass(frozen=True)
class RouteEntry:
    contract_code: str
    server: str
    topic: str
    option_type: str
    strike: str = ""
    source_file: str = ""
    underlying: str = ""  # 标的代码，由 wxy_* 映射文件所属品种决定，用于校验粘贴错误


class DDERouteParser:
    """
    解析交易软件导出的 DDE Excel 路由表（xlsx 内部 XML）。

    说明：
    - 不依赖 openpyxl/pandas，直接解析 xlsx 压缩包中的 XML
    - 目标输出为：{contract_code: topic}
    - 同时保留完整 route entries，供 DDE 连接时获取 server/topic
    """

    # 根据已验证样本，使用这两列可稳定拿到 call/put 对应 topic
    _CALL_CONTRACT_COL = "K"
    _PUT_CONTRACT_COL = "M"
    _STRIKE_COL = "L"
    _CALL_FORMULA_COLS = ("D", "A", "I")
    _PUT_FORMULA_COLS = ("T", "O", "W")
    _ETF_CODE_COL = "A"
    _ETF_FORMULA_COLS = ("H", "I", "D")

    def __init__(self, excel_files: Dict[str, str], logger: Optional[logging.Logger] = None) -> None:
        self.excel_files = {k: str(v) for k, v in excel_files.items()}
        self.logger = logger or logging.getLogger(self.__class__.__name__)
        self.routes: Dict[str, RouteEntry] = {}

    def parse(self) -> Dict[str, str]:
        """解析全部 Excel，并返回 contract_code -> topic。"""
        merged: Dict[str, RouteEntry] = {}
        for asset, file_path in self.excel_files.items():
            path = Path(file_path)
            if not path.exists():
                self.logger.warning("映射文件不存在，跳过: %s (%s)", path, asset)
                continue

            entries = self._parse_single_file(path, underlying=asset)
            for code, entry in entries.items():
                if code in merged and merged[code].topic != entry.topic:
                    self.logger.warning(
                        "合约 %s 在多个文件中 topic 不一致，保留最新值 %s -> %s",
                        code,
                        merged[code].topic,
                        entry.topic,
                    )
                merged[code] = entry

            self.logger.info(
                "解析完成: %s, 识别合约 %d 个",
                path.name,
                len(entries),
            )

        self.routes = merged
        return {code: entry.topic for code, entry in merged.items()}

    def _parse_single_file(self, xlsx_path: Path, underlying: str = "") -> Dict[str, RouteEntry]:
        with zipfile.ZipFile(xlsx_path, "r") as zf:
            shared_strings = self._read_shared_strings(zf)
            link_map = self._read_external_links(zf)
            if not link_map:
                self.logger.warning("未发现 externalLink，文件可能不是 DDE 路由表: %s", xlsx_path.name)
            sheet_defs = self._read_workbook_sheets(zf)
            routes: Dict[str, RouteEntry] = {}
            for sheet_name, sheet_path in sheet_defs:
                sheet_routes = self._read_sheet_routes(
                    zf=zf,
                    shared_strings=shared_strings,
                    link_map=link_map,
                    source_name=xlsx_path.name,
                    sheet_name=sheet_name,
                    sheet_path=sheet_path,
                    underlying=underlying,
                )
                if sheet_routes:
                    self.logger.info(
                        "文件 %s / sheet %s 解析到 %d 条路由",
                        xlsx_path.name,
                        sheet_name,
                        len(sheet_routes),
                    )
                routes.update(sheet_routes)
            return routes

    def _read_workbook_sheets(self, zf: zipfile.ZipFile) -> List[Tuple[str, str]]:
        workbook_file = "xl/workbook.xml"
        rels_file = "xl/_rels/workbook.xml.rels"
        if workbook_file not in zf.namelist() or rels_file not in zf.namelist():
            # 兜底：老格式只按 sheet1 读取
            return [("sheet1", "xl/worksheets/sheet1.xml")]

        wb_root = ET.fromstring(zf.read(workbook_file))
        rel_root = ET.fromstring(zf.read(rels_file))

        rel_map: Dict[str, str] = {}
        for rel in rel_root.findall(".//r:Relationship", _NS_REL):
            rid = (rel.attrib.get("Id") or "").strip()
            target = (rel.attrib.get("Target") or "").strip()
            if rid and target:
                rel_map[rid] = target

        out: List[Tuple[str, str]] = []
        for sheet in wb_root.findall(".//s:sheets/s:sheet", _NS_MAIN):
            name = (sheet.attrib.get("name") or "").strip()
            rid = sheet.attrib.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id", "")
            target = rel_map.get(rid, "")
            if not target:
                continue
            if not target.startswith("worksheets/"):
                continue
            path = f"xl/{target}"
            if path in zf.namelist():
                out.append((name or Path(path).stem, path))

        if not out:
            return [("sheet1", "xl/worksheets/sheet1.xml")]
        return out

    def _read_shared_strings(self, zf: zipfile.ZipFile) -> List[str]:
        if "xl/sharedStrings.xml" not in zf.namelist():
            return []
        try:
            root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
        except Exception as exc:
            self.logger.warning("读取 sharedStrings 失败: %s", exc)
            return []

        values: List[str] = []
        for si in root.findall(".//s:si", _NS_MAIN):
            text_parts = [t.text or "" for t in si.findall(".//s:t", _NS_MAIN)]
            values.append("".join(text_parts))
        return values

    def _read_external_links(self, zf: zipfile.ZipFile) -> Dict[int, Tuple[str, str]]:
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
            except Exception as exc:
                self.logger.warning("解析 externalLink 失败: %s, err=%s", name, exc)
                continue
            dde_link = root.find(".//s:ddeLink", _NS_MAIN)
            if dde_link is None:
                continue
            server = (dde_link.get("ddeService") or "").strip()
            topic = (dde_link.get("ddeTopic") or "").strip()
            if server and topic:
                out[idx] = (server, topic)
        return out

    def _read_sheet_routes(
        self,
        zf: zipfile.ZipFile,
        shared_strings: List[str],
        link_map: Dict[int, Tuple[str, str]],
        source_name: str,
        sheet_name: str,
        sheet_path: str,
        underlying: str = "",
    ) -> Dict[str, RouteEntry]:
        if sheet_path not in zf.namelist():
            self.logger.warning("缺少 %s，跳过文件 %s", sheet_path, source_name)
            return {}

        root = ET.fromstring(zf.read(sheet_path))
        rows = root.findall(".//s:sheetData/s:row", _NS_MAIN)
        routes: Dict[str, RouteEntry] = {}
        source_tag = f"{source_name}#{sheet_name}"

        for row in rows[1:]:  # 跳过标题行
            cell_map = self._row_to_cell_map(row, shared_strings)
            if not cell_map:
                continue

            call_code = self._safe_text(cell_map, self._CALL_CONTRACT_COL)
            put_code = self._safe_text(cell_map, self._PUT_CONTRACT_COL)
            strike = self._safe_text(cell_map, self._STRIKE_COL)

            # 期权表（K/M 为期权合约代码，通常是 8 位数字）
            is_option_row = self._is_option_contract_code(call_code) or self._is_option_contract_code(put_code)
            if is_option_row:
                call_server, call_topic = self._resolve_server_topic(cell_map, link_map, self._CALL_FORMULA_COLS)
                put_server, put_topic = self._resolve_server_topic(cell_map, link_map, self._PUT_FORMULA_COLS)

                if call_code and call_server and call_topic:
                    routes[call_code] = RouteEntry(
                        contract_code=call_code,
                        server=call_server,
                        topic=call_topic,
                        option_type="CALL",
                        strike=strike,
                        source_file=source_tag,
                        underlying=underlying,
                    )
                if put_code and put_server and put_topic:
                    routes[put_code] = RouteEntry(
                        contract_code=put_code,
                        server=put_server,
                        topic=put_topic,
                        option_type="PUT",
                        strike=strike,
                        source_file=source_tag,
                        underlying=underlying,
                    )
                continue

            # ETF 表（A 是 ETF 代码，如 510050）
            etf_code_raw = self._safe_text(cell_map, self._ETF_CODE_COL)
            if not etf_code_raw:
                continue
            if not etf_code_raw.isdigit():
                continue
            etf_server, etf_topic = self._resolve_server_topic(cell_map, link_map, self._ETF_FORMULA_COLS)
            if etf_server and etf_topic:
                etf_code = self._normalize_underlying_code(etf_code_raw)
                routes[etf_code] = RouteEntry(
                    contract_code=etf_code,
                    server=etf_server,
                    topic=etf_topic,
                    option_type="ETF",
                    strike=strike,
                    source_file=source_tag,
                    underlying=underlying or etf_code,
                )
        return routes

    @staticmethod
    def _normalize_underlying_code(code: str) -> str:
        c = code.strip()
        if "." in c:
            return c
        return f"{c}.SH"

    @staticmethod
    def _is_option_contract_code(code: str) -> bool:
        c = (code or "").strip()
        return c.isdigit() and len(c) >= 8

    def _row_to_cell_map(self, row_elem: ET.Element, shared_strings: List[str]) -> Dict[str, Dict[str, str]]:
        out: Dict[str, Dict[str, str]] = {}
        for cell in row_elem.findall("s:c", _NS_MAIN):
            ref = cell.get("r", "")
            col_match = _COL_RE.match(ref)
            if not col_match:
                continue
            col = col_match.group(1)
            t_attr = (cell.get("t") or "").strip()
            v_elem = cell.find("s:v", _NS_MAIN)
            f_elem = cell.find("s:f", _NS_MAIN)

            val = v_elem.text if v_elem is not None and v_elem.text is not None else ""
            formula = f_elem.text if f_elem is not None and f_elem.text is not None else ""

            if t_attr == "s" and val:
                try:
                    idx = int(val)
                    val = shared_strings[idx] if 0 <= idx < len(shared_strings) else ""
                except ValueError:
                    val = ""
            out[col] = {"val": str(val).strip(), "formula": str(formula).strip()}
        return out

    @staticmethod
    def _safe_text(cell_map: Dict[str, Dict[str, str]], col: str) -> str:
        return (cell_map.get(col, {}).get("val") or "").strip()

    def _resolve_server_topic(
        self,
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


class DDEClientManager:
    """DDE 连接池，支持 REQUEST（轮询）和 ADVISE（热链接推送）两种模式。"""

    def __init__(
        self,
        app_name: str = "DeltaZeroDDE",
        request_fields: Optional[List[str]] = None,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self.logger = logger or logging.getLogger(self.__class__.__name__)
        self.app_name = app_name
        self.request_fields = request_fields or [
            "BIDPRICE1",
            "ASKPRICE1",
            "BIDVOLUME1",
            "ASKVOLUME1",
            "LASTPRICE",
        ]

        self._u32: Any = None
        self._idInst = wintypes.DWORD(0)
        self._callback: Any = None
        self._convs: Dict[str, ctypes.c_void_p] = {}
        self._topic_server: Dict[str, str] = {}
        self._lock = threading.Lock()
        self._initialized = False

        # ADVISE 模式数据结构
        self._advise_lock = threading.Lock()
        self._advise_snapshot: Dict[str, Dict[str, Optional[float]]] = {}  # topic → {field → value}
        self._dirty_topics: set = set()
        self._topic_to_codes: Dict[str, List[str]] = {}  # topic → [contract_codes]

        self._init_dde()

    @property
    def initialized(self) -> bool:
        return self._initialized

    def _dde_callback(self, uType, uFmt, hconv, hsz1, hsz2, hdata, dw1, dw2):
        if uType == _XTYP_ADVDATA:
            try:
                topic = self._query_string(hsz1)
                item = self._query_string(hsz2)
                if topic and item:
                    value = self._read_advise_data(hdata)
                    if value is not None and "VOLUME" in item.upper():
                        value = float(int(round(value)))
                    with self._advise_lock:
                        self._advise_snapshot.setdefault(topic, {})[item] = value
                        self._dirty_topics.add(topic)
            except Exception:
                pass
            return _DDE_FACK
        return 0

    def _query_string(self, hsz: ctypes.c_void_p) -> str:
        """将 HSZ 句柄还原为字符串。"""
        buf = ctypes.create_unicode_buffer(256)
        length = self._u32.DdeQueryStringW(self._idInst, hsz, buf, 256, _CP_WINUNICODE)
        if length > 0:
            return buf.value
        return ""

    def _read_advise_data(self, hdata: ctypes.c_void_p) -> Optional[float]:
        """从 ADVISE 回调中的 hdata 句柄读取并解析数据（不释放句柄，系统拥有）。"""
        if not hdata:
            return None
        cb = wintypes.DWORD(0)
        pData = self._u32.DdeAccessData(hdata, ctypes.byref(cb))
        if not pData or cb.value == 0:
            return None
        try:
            raw = ctypes.string_at(pData, cb.value)
        finally:
            self._u32.DdeUnaccessData(hdata)
        return _parse_dde_response(raw)

    def _init_dde(self) -> None:
        try:
            self._u32 = _setup_ddeml()
            self._callback = _DDECALLBACK(self._dde_callback)
            ret = self._u32.DdeInitializeW(
                ctypes.byref(self._idInst),
                self._callback,
                _APPCMD_CLIENTONLY,
                0,
            )
            if ret != 0:
                self.logger.error("DdeInitialize 失败, 返回码: %d", ret)
                self._initialized = False
                return
            self._initialized = True
            self.logger.info("DDE (ctypes DDEML) 初始化成功, idInst=%d", self._idInst.value)
        except Exception as exc:
            self.logger.error("DDE 初始化异常: %s", exc)
            self._initialized = False

    def _make_hsz(self, text: str) -> ctypes.c_void_p:
        return self._u32.DdeCreateStringHandleW(self._idInst, text, _CP_WINUNICODE)

    def _free_hsz(self, hsz: ctypes.c_void_p) -> None:
        if hsz:
            self._u32.DdeFreeStringHandle(self._idInst, hsz)

    def _pump_messages(self) -> None:
        msg = _MSG()
        while self._u32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
            self._u32.TranslateMessage(ctypes.byref(msg))
            self._u32.DispatchMessageW(ctypes.byref(msg))

    def connect_topic(self, server: str, topic: str) -> bool:
        if not self._initialized:
            return False
        if topic in self._convs:
            return True
        hsz_srv = self._make_hsz(server)
        hsz_top = self._make_hsz(topic)
        try:
            hConv = self._u32.DdeConnect(self._idInst, hsz_srv, hsz_top, None)
            self._pump_messages()
            if hConv:
                self._convs[topic] = hConv
                self._topic_server[topic] = server
                return True
            err = self._u32.DdeGetLastError(self._idInst)
            self.logger.warning("DDE Connect 失败: server=%s topic=%s err=0x%04x", server, topic, err)
            return False
        finally:
            self._free_hsz(hsz_srv)
            self._free_hsz(hsz_top)

    def connect_routes(self, routes: Dict[str, RouteEntry]) -> Tuple[int, int]:
        unique_topics: Dict[str, str] = {}
        for entry in routes.values():
            unique_topics[entry.topic] = entry.server
        total = len(unique_topics)
        ok = sum(1 for t, s in unique_topics.items() if self.connect_topic(s, t))
        self.logger.info("DDE 连接建立完成: %d/%d", ok, total)
        return ok, total

    def reconnect_topic(self, topic: str) -> bool:
        server = self._topic_server.get(topic)
        if not server:
            return False
        with self._lock:
            old = self._convs.pop(topic, None)
            if old:
                self._u32.DdeDisconnect(old)
            return self.connect_topic(server, topic)

    def _request_raw(self, topic: str, item: str) -> Optional[bytes]:
        """Send a DDE Request and return the raw byte payload."""
        hConv = self._convs.get(topic)
        if not hConv:
            return None
        hsz_item = self._make_hsz(item)
        try:
            hData = self._u32.DdeClientTransaction(
                None, 0, hConv, hsz_item, _CF_TEXT,
                _XTYP_REQUEST, _DDE_TIMEOUT_MS, None,
            )
            self._pump_messages()
            if not hData:
                return None
            cb = wintypes.DWORD(0)
            pData = self._u32.DdeAccessData(hData, ctypes.byref(cb))
            if pData and cb.value > 0:
                raw = ctypes.string_at(pData, cb.value)
            else:
                raw = b""
            self._u32.DdeUnaccessData(hData)
            self._u32.DdeFreeDataHandle(hData)
            return raw
        finally:
            self._free_hsz(hsz_item)

    def request(self, topic: str, item: str) -> Optional[float]:
        """Request a single DDE item and return parsed numeric value."""
        if not self._initialized:
            return None
        raw = self._request_raw(topic, item)
        if raw is None:
            return None
        return _parse_dde_response(raw)

    def request_diagnostic(
        self, topic: str, item: str,
    ) -> Tuple[Optional[float], str, Optional[str], list]:
        """
        诊断模式：返回 (parsed_value, raw_hex, error_msg, raw_byte_list)。
        """
        if not self._initialized:
            return None, "", "dde_not_initialized", []
        if topic not in self._convs:
            return None, "", "topic_not_connected", []
        raw = self._request_raw(topic, item)
        if raw is None:
            err = self._u32.DdeGetLastError(self._idInst)
            return None, "", f"request_failed(0x{err:04x})", []
        hex_display = raw.hex(" ")
        parsed = _parse_dde_response(raw)
        return parsed, hex_display, None, list(raw)

    def poll_data(self, routes: Dict[str, RouteEntry]) -> Dict[str, Dict[str, Optional[float]]]:
        out: Dict[str, Dict[str, Optional[float]]] = {}
        for code, entry in routes.items():
            row: Dict[str, Optional[float]] = {}
            for field in self.request_fields:
                val = self.request(entry.topic, field)
                if val is not None and "VOLUME" in field.upper():
                    val = float(int(round(val)))
                row[field] = val
            out[code] = row
        return out

    # ------------------------------------------------------------------
    # ADVISE（热链接）模式
    # ------------------------------------------------------------------

    def advise_start_all(self, routes: Dict[str, RouteEntry]) -> Tuple[int, int]:
        """对所有 (topic, field) 注册 ADVISE 热链接。返回 (成功数, 总数)。"""
        if not self._initialized:
            return 0, 0
        total = ok = 0
        # 建立 topic → codes 反向映射
        self._topic_to_codes.clear()
        for code, entry in routes.items():
            self._topic_to_codes.setdefault(entry.topic, []).append(code)

        for topic, hConv in self._convs.items():
            for field in self.request_fields:
                total += 1
                hsz_item = self._make_hsz(field)
                try:
                    hData = self._u32.DdeClientTransaction(
                        None, 0, hConv, hsz_item, _CF_TEXT,
                        _XTYP_ADVSTART, _DDE_TIMEOUT_MS, None,
                    )
                    self._pump_messages()
                    if hData:
                        ok += 1
                    else:
                        err = self._u32.DdeGetLastError(self._idInst)
                        self.logger.debug(
                            "ADVISE 注册失败: topic=%s item=%s err=0x%04x", topic, field, err,
                        )
                finally:
                    self._free_hsz(hsz_item)
        self.logger.info("ADVISE 热链接注册: %d/%d 成功", ok, total)
        return ok, total

    def advise_stop_all(self) -> None:
        """取消所有 ADVISE 热链接。"""
        if not self._initialized:
            return
        for topic, hConv in self._convs.items():
            for field in self.request_fields:
                hsz_item = self._make_hsz(field)
                try:
                    self._u32.DdeClientTransaction(
                        None, 0, hConv, hsz_item, _CF_TEXT,
                        _XTYP_ADVSTOP, _DDE_TIMEOUT_MS, None,
                    )
                except Exception:
                    pass
                finally:
                    self._free_hsz(hsz_item)
        self._pump_messages()

    def pump_and_collect(self) -> Dict[str, Dict[str, Optional[float]]]:
        """泵 Windows 消息（触发回调），返回有变化的 {topic: {field: value}}。"""
        self._pump_messages()
        with self._advise_lock:
            if not self._dirty_topics:
                return {}
            out = {}
            for topic in self._dirty_topics:
                snapshot = self._advise_snapshot.get(topic)
                if snapshot:
                    out[topic] = dict(snapshot)
            self._dirty_topics.clear()
            return out

    def get_full_snapshot(self) -> Dict[str, Dict[str, Optional[float]]]:
        """返回所有 topic 的当前快照（用于 staleness 检查）。"""
        with self._advise_lock:
            return {t: dict(s) for t, s in self._advise_snapshot.items()}

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def close(self) -> None:
        self.advise_stop_all()
        with self._lock:
            for hConv in self._convs.values():
                try:
                    self._u32.DdeDisconnect(hConv)
                except Exception:
                    pass
            self._convs.clear()
            self._topic_server.clear()
            self._topic_to_codes.clear()
            if self._idInst.value:
                try:
                    self._u32.DdeUninitialize(self._idInst)
                except Exception:
                    pass
                self._idInst = wintypes.DWORD(0)
            self._initialized = False


class DDEDataFeeder:
    """
    DDE 数据采集主入口：
    1) 解析 Excel 路由
    2) 建立 DDE 连接池
    3) 轮询实时盘口
    """

    def __init__(
        self,
        excel_files: Dict[str, str],
        poll_interval: float = 3.0,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self.logger = logger or logging.getLogger(self.__class__.__name__)
        self.excel_files = excel_files
        self.poll_interval = poll_interval

        self.parser = DDERouteParser(excel_files, logger=self.logger)
        self.client = DDEClientManager(logger=self.logger)
        self.routes: Dict[str, RouteEntry] = {}
        self.started = False

    def start(self) -> bool:
        contract_to_topic = self.parser.parse()
        self.routes = dict(self.parser.routes)
        total_routes = len(contract_to_topic)
        if total_routes == 0:
            self.logger.error("未解析到任何合约路由，启动失败")
            return False

        ok, total_topics = self.client.connect_routes(self.routes)
        self.logger.info(
            "路由解析成功率: %d 合约, DDE 连接成功率: %d/%d topic",
            total_routes,
            ok,
            total_topics,
        )
        self.started = self.client.initialized and ok > 0
        return self.started

    def poll_once(self) -> Dict[str, Dict[str, Optional[float]]]:
        if not self.started:
            raise RuntimeError("DDEDataFeeder 未启动，请先调用 start()")
        return self.client.poll_data(self.routes)

    def run_loop(
        self,
        interval: Optional[float] = None,
        stop_file: Optional[Path] = None,
    ) -> None:
        if interval is None:
            interval = self.poll_interval
        stop_path = stop_file or _DEFAULT_STOP_FILE
        self.logger.info("开始 DDE 轮询, 间隔 %.2f 秒, 停止信号文件: %s", interval, stop_path)
        if stop_path.exists():
            stop_path.unlink()
        try:
            while True:
                if stop_path.exists():
                    self.logger.info("检测到停止信号文件，退出轮询")
                    break
                data = self.poll_once()
                self.logger.info("本轮抓取完成: %d 合约", len(data))
                elapsed = 0.0
                while elapsed < interval:
                    if stop_path.exists():
                        break
                    time.sleep(min(1.0, interval - elapsed))
                    elapsed += 1.0
                if stop_path.exists():
                    break
        except KeyboardInterrupt:
            self.logger.info("收到中断信号，停止轮询")
        finally:
            self.stop()

    def stop(self) -> None:
        self.client.close()
        self.started = False
        self.logger.info("DDEDataFeeder 已停止")


def _build_default_excel_files() -> Dict[str, str]:
    """
    默认文件名约定（如存在则自动启用）：
    - metadata/wxy_50etf.xlsx
    - metadata/wxy_300etf.xlsx
    - metadata/wxy_500etf.xlsx
    """
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


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    logger = logging.getLogger("DDEDataFeederMain")

    parser = argparse.ArgumentParser(
        description="DDE 期权盘口数据采集",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python -m data_engine.DDE_Data_Feeder               # 单次抓取后退出
  python -m data_engine.DDE_Data_Feeder --loop        # 持续轮询（后台运行）
  python -m data_engine.DDE_Data_Feeder --loop -i 5   # 每 5 秒轮询一次
  python -m data_engine.DDE_Data_Feeder --stop         # 发送停止信号，让正在运行的 feeder 退出
        """,
    )
    parser.add_argument(
        "--loop",
        action="store_true",
        help="持续轮询模式，按 Ctrl+C 或执行 --stop 可停止",
    )
    parser.add_argument(
        "-i", "--interval",
        type=float,
        default=3.0,
        help="轮询间隔（秒），默认 3",
    )
    parser.add_argument(
        "--stop",
        action="store_true",
        help="创建停止信号文件，使正在运行的 feeder 退出（需 feeder 以 --loop 启动）",
    )
    args = parser.parse_args()

    if args.stop:
        _DEFAULT_STOP_FILE.parent.mkdir(parents=True, exist_ok=True)
        _DEFAULT_STOP_FILE.touch()
        logger.info("已创建停止信号文件: %s，feeder 将在下一轮检测后退出", _DEFAULT_STOP_FILE)
        raise SystemExit(0)

    excel_files = _build_default_excel_files()
    if not excel_files:
        logger.error("未找到默认映射文件，请至少准备 metadata/wxy_50etf.xlsx")
        raise SystemExit(1)

    feeder = DDEDataFeeder(excel_files=excel_files, poll_interval=args.interval, logger=logger)
    if not feeder.start():
        logger.error("启动失败，请检查 DDE 环境/映射文件")
        raise SystemExit(2)

    if args.loop:
        feeder.run_loop(interval=args.interval)
    else:
        try:
            first = feeder.poll_once()
            logger.info("首次抓取成功，样例前5条:")
            for idx, (code, row) in enumerate(first.items()):
                if idx >= 5:
                    break
                logger.info(
                    "%s LAST=%.6f BID1=%.6f ASK1=%.6f B1V=%.0f A1V=%.0f",
                    code,
                    row.get("LASTPRICE") or float("nan"),
                    row.get("BIDPRICE1") or float("nan"),
                    row.get("ASKPRICE1") or float("nan"),
                    row.get("BIDVOLUME1") or float("nan"),
                    row.get("ASKVOLUME1") or float("nan"),
                )
        except Exception as exc:
            logger.warning("首次抓取失败: %s", exc)
        feeder.stop()
