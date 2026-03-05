"""
Wind API 适配器

封装 Wind 终端的实时推送（wsq）和历史数据提取（wsd）接口。
在无 Wind 环境下优雅降级为 Mock 模式，不影响回测流程。

注意：使用本模块需要已安装 WindPy 并登录 Wind 终端。

说明：
  本模块保留为通用 Wind 访问封装（实时/历史/合约查询）。
  生产实时总线请优先使用 data_bus/wind_subscriber.py。
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any, Callable, Dict, List, Optional

from models import TickData, ContractInfo, normalize_code

logger = logging.getLogger(__name__)


# Wind API 延迟导入，无 Wind 环境不抛异常
_wind_available = False
_w = None

try:
    from WindPy import w as _wind_module
    _w = _wind_module
    _wind_available = True
except ImportError:
    logger.info("WindPy 未安装，Wind 适配器将工作在 Mock 模式")


class WindAdapter:
    """
    Wind 金融终端数据适配器

    提供实时行情订阅和历史数据查询两大功能，
    数据统一转换为系统内部的 TickData / dict 格式。

    Attributes:
        is_connected: 是否已连接 Wind 终端
        mock_mode: 是否处于 Mock 模式（无 Wind 环境）
    """

    def __init__(self, timeout: int = 30) -> None:
        """
        初始化 Wind 适配器

        Args:
            timeout: Wind API 连接超时时间（秒）
        """
        self._timeout = timeout
        self._connected = False
        self._mock_mode = not _wind_available
        self._subscriptions: Dict[str, Callable] = {}

        if self._mock_mode:
            logger.warning("Wind 适配器运行在 Mock 模式，所有 API 调用返回空数据")

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def mock_mode(self) -> bool:
        return self._mock_mode

    def connect(self) -> bool:
        """
        连接 Wind 终端

        Returns:
            是否连接成功
        """
        if self._mock_mode:
            logger.info("[Mock] 模拟 Wind 连接成功")
            self._connected = True
            return True

        try:
            result = _w.start(waitTime=self._timeout)
            if result.ErrorCode == 0:
                self._connected = True
                logger.info("Wind 终端连接成功")
                return True
            else:
                logger.error("Wind 连接失败，错误码: %s", result.ErrorCode)
                return False
        except Exception as e:
            logger.error("Wind 连接异常: %s", e)
            return False

    def disconnect(self) -> None:
        """断开 Wind 连接"""
        if self._mock_mode:
            self._connected = False
            return

        try:
            if self._subscriptions:
                _w.cancelRequest(0)
                self._subscriptions.clear()
            _w.stop()
            self._connected = False
            logger.info("Wind 终端已断开")
        except Exception as e:
            logger.error("Wind 断开异常: %s", e)

    def subscribe_realtime(
        self,
        codes: List[str],
        callback: Callable[[TickData], None],
        fields: str = "rt_last,rt_ask1,rt_bid1,rt_ask_vol1,rt_bid_vol1,rt_vol,rt_amt,rt_oi",
    ) -> bool:
        """
        订阅实时行情推送（wsq）

        每当行情更新时，将数据转换为 TickData 并调用 callback。

        Args:
            codes: 合约代码列表（Wind 格式，如 ['10000001.SH']）
            callback: 行情更新回调函数
            fields: 订阅字段

        Returns:
            是否订阅成功
        """
        if self._mock_mode:
            logger.info("[Mock] 模拟订阅 %d 个合约", len(codes))
            for code in codes:
                self._subscriptions[code] = callback
            return True

        self._ensure_connected()

        codes_str = ",".join(codes)

        def _wsq_callback(indata: Any) -> None:
            """wsq 内部回调，将 Wind 数据转换为 TickData"""
            try:
                if indata.ErrorCode != 0:
                    logger.warning("wsq 推送错误: %s", indata.ErrorCode)
                    return

                for i, code in enumerate(indata.Codes):
                    tick = self._wind_data_to_tick(code, indata.Fields, indata.Data, i)
                    if tick is not None:
                        callback(tick)
            except Exception as e:
                logger.error("wsq 回调处理异常: %s", e)

        try:
            _w.wsq(codes_str, fields, func=_wsq_callback)
            for code in codes:
                self._subscriptions[code] = callback
            logger.info("成功订阅 %d 个合约的实时行情", len(codes))
            return True
        except Exception as e:
            logger.error("wsq 订阅失败: %s", e)
            return False

    def get_historical_data(
        self,
        code: str,
        start_date: date,
        end_date: date,
        fields: str = "close,high,low,volume,amt,oi",
        bar_size: str = "",
    ) -> Optional[Dict[str, List]]:
        """
        获取历史行情数据（wsd）

        Args:
            code: 合约代码（Wind 格式）
            start_date: 开始日期
            end_date: 结束日期
            fields: 查询字段
            bar_size: K线周期（空字符串为日线）

        Returns:
            字段名 -> 数据列表的字典，失败返回 None
        """
        if self._mock_mode:
            logger.info("[Mock] 模拟查询 %s 历史数据: %s ~ %s", code, start_date, end_date)
            return {"times": [], "data": {f: [] for f in fields.split(",")}}

        self._ensure_connected()

        try:
            options = f"BarSize={bar_size}" if bar_size else ""
            result = _w.wsd(
                code, fields,
                start_date.strftime("%Y-%m-%d"),
                end_date.strftime("%Y-%m-%d"),
                options,
            )
            if result.ErrorCode != 0:
                logger.error("wsd 查询失败 [%s]: 错误码 %s", code, result.ErrorCode)
                return None

            data = {}
            field_list = fields.split(",")
            data["times"] = result.Times
            for i, f in enumerate(field_list):
                data[f.strip()] = result.Data[i]

            return data
        except Exception as e:
            logger.error("wsd 查询异常 [%s]: %s", code, e)
            return None

    def query_contract_info(self, code: str) -> Optional[Dict[str, Any]]:
        """
        查询合约基本信息（wss）

        可作为 ContractInfoManager 的补充数据源。

        Args:
            code: 合约代码

        Returns:
            合约属性字典，失败返回 None
        """
        if self._mock_mode:
            logger.info("[Mock] 模拟查询 %s 合约信息", code)
            return None

        self._ensure_connected()

        try:
            fields = "exe_price,exe_enddate,exe_type,contractmultiplier,us_code"
            result = _w.wss(code, fields)
            if result.ErrorCode != 0:
                logger.error("wss 查询失败 [%s]: 错误码 %s", code, result.ErrorCode)
                return None

            field_list = [f.strip() for f in fields.split(",")]
            return {f: result.Data[i][0] for i, f in enumerate(field_list)}
        except Exception as e:
            logger.error("wss 查询异常 [%s]: %s", code, e)
            return None

    def _ensure_connected(self) -> None:
        """确保已连接 Wind，否则自动尝试连接"""
        if not self._connected:
            if not self.connect():
                raise ConnectionError("无法连接 Wind 终端")

    @staticmethod
    def _wind_data_to_tick(
        code: str,
        fields: List[str],
        data: List[List],
        col_idx: int,
    ) -> Optional[TickData]:
        """将 Wind wsq 推送的原始数据转换为 TickData"""
        try:
            field_map = {}
            for i, f_name in enumerate(fields):
                field_map[f_name.upper()] = data[i][col_idx] if col_idx < len(data[i]) else None

            import math
            return TickData(
                timestamp=datetime.now(),
                contract_code=normalize_code(code, ".SH"),
                current=float(field_map.get("RT_LAST", 0) or 0),
                volume=int(field_map.get("RT_VOL", 0) or 0),
                high=float(field_map.get("RT_LAST", 0) or 0),
                low=float(field_map.get("RT_LAST", 0) or 0),
                money=float(field_map.get("RT_AMT", 0) or 0),
                position=int(field_map.get("RT_OI", 0) or 0),
                ask_prices=[float(field_map.get("RT_ASK1", math.nan) or math.nan)] + [math.nan] * 4,
                ask_volumes=[int(field_map.get("RT_ASK_VOL1", 0) or 0)] + [0] * 4,
                bid_prices=[float(field_map.get("RT_BID1", math.nan) or math.nan)] + [math.nan] * 4,
                bid_volumes=[int(field_map.get("RT_BID_VOL1", 0) or 0)] + [0] * 4,
            )
        except Exception as e:
            logger.warning("Wind 数据转换失败 [%s]: %s", code, e)
            return None
