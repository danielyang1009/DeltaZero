"""
全局配置模块

集中管理交易费率、滑点参数、保证金比例、数据路径等系统级配置。
所有数值参数均可在实例化时覆盖默认值。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List


UNDERLYINGS: List[str] = [
    "510050.SH",
    "510300.SH",
    "510500.SH",
]

ETF_CODE_TO_NAME: Dict[str, str] = {
    "510050.SH": "50ETF",
    "510300.SH": "300ETF",
    "510500.SH": "500ETF",
}

DEFAULT_MARKET_DATA_DIR = r"D:\MARKET_DATA"

# Monitor 显示参数默认值（web 控制台启动面板的初始值，用户可在页面修改）
DEFAULT_MIN_PROFIT   = 36.0   # 净利润显示门槛（元/组）
DEFAULT_EXPIRY_DAYS  = 90     # 最大到期天数
DEFAULT_N_EACH_SIDE  = 0      # ATM 上下各显示 N 组（0 = 全部）
DEFAULT_REFRESH_SECS = 3      # 刷新间隔（秒）


@dataclass
class FeeConfig:
    """
    交易费用配置（仅回测引擎使用）

    实时监控的简化费率参数见 TradingConfig.etf_fee_rate / option_round_trip_fee。
    """
    option_commission_per_contract: float = 1.7   # 期权每张手续费（元）
    option_commission_rate: float = 0.0            # 期权按比例手续费（备用）
    etf_commission_rate: float = 0.00006           # ETF 佣金费率（万0.6）
    etf_min_commission: float = 0.1                # ETF 最低佣金（元）
    stamp_tax_rate: float = 0.0                    # 印花税（ETF 免征）
    exercise_fee_per_contract: float = 0.6         # 行权手续费（每张）


@dataclass
class SlippageConfig:
    """
    滑点配置（仅回测引擎使用）

    实时监控使用严格 Bid/Ask 吃单价格，不额外计算滑点。
    """
    option_slippage_ticks: int = 1                 # 期权滑点（最小变动单位数）
    etf_slippage_ticks: int = 1                    # ETF 滑点
    option_tick_size: float = 0.0001               # 期权最小变动价位
    etf_tick_size: float = 0.001                   # ETF 最小变动价位


@dataclass
class MarginConfig:
    """
    上交所期权保证金参数

    卖出开仓保证金 = 权利金 + max(比例1 * 标的价格 - 虚值额, 比例2 * 标的价格或行权价)
    具体比例根据品种和交易所规则设定。
    """
    call_margin_ratio_1: float = 0.12              # 认购保证金比例1
    call_margin_ratio_2: float = 0.07              # 认购保证金比例2
    put_margin_ratio_1: float = 0.12               # 认沽保证金比例1
    put_margin_ratio_2: float = 0.07               # 认沽保证金比例2


@dataclass
class TradingConfig:
    """
    系统主配置

    汇总所有子配置项，提供统一的配置入口。
    """
    # 子配置
    fee: FeeConfig = field(default_factory=FeeConfig)
    slippage: SlippageConfig = field(default_factory=SlippageConfig)
    margin: MarginConfig = field(default_factory=MarginConfig)

    # 市场参数
    risk_free_rate: float = 0.02                   # 无风险利率（年化）
    contract_unit: int = 10000                     # ETF 期权合约单位
    trading_days_per_year: int = 252               # 年交易日数

    # 回测参数
    initial_capital: float = 1_000_000.0           # 初始资金（元）
    max_position_per_signal: int = 10              # 单信号最大开仓组数

    # 数据路径
    data_paths: Dict[str, str] = field(default_factory=lambda: {
        "sample_data": "sample_data",
        "info_data": "metadata",
    })

    # Wind 连接（实盘模式）
    wind_enabled: bool = False
    wind_timeout: int = 30                         # Wind API 超时（秒）

    # 信号过滤阈值
    min_profit_threshold: float = 50.0             # 最小净利润阈值（元/组）
    min_volume_threshold: int = 10                 # 最小成交量过滤
    enable_reverse: bool = False                   # 是否输出反向套利信号（融券卖出，未计息成本，默认关闭）

    # PCP 套利成本参数（实时监控简化公式，见 pcp_arbitrage.py 说明）
    etf_fee_rate: float = 0.00020                  # ETF 现货单边规费（含佣金+过户费，约万2）
    option_round_trip_fee: float = 3.0             # 期权双边固定手续费（≈ fee.option_commission_per_contract × 2 取整）
    include_interest: bool = False                 # 是否计入持有到期利息成本（K×(1-e^(-rT))×mult）

    # ETF 模拟器参数
    simulation_volatility: float = 0.20            # 模拟波动率
    simulation_drift: float = 0.03                 # 模拟漂移率

    def __post_init__(self) -> None:
        assert self.contract_unit > 0, "contract_unit must be positive"
        assert 0 <= self.etf_fee_rate < 0.01, f"etf_fee_rate={self.etf_fee_rate} out of range [0, 0.01)"
        assert self.option_round_trip_fee >= 0, "option_round_trip_fee must be non-negative"
        assert self.min_profit_threshold >= 0, "min_profit_threshold must be non-negative"
        assert self.initial_capital > 0, "initial_capital must be positive"


@dataclass
class RecorderConfig:
    """
    数据记录进程配置

    控制 Wind 订阅品种、存储路径、分片写入间隔、ZMQ 发布端口等。
    """
    products: List[str] = field(default_factory=lambda: list(UNDERLYINGS))

    # Wind 订阅字段
    option_fields: str = "rt_last,rt_ask1,rt_bid1,rt_oi,rt_vol,rt_high,rt_low"
    etf_fields: str    = "rt_last,rt_ask1,rt_bid1"

    # 存储路径（统一为 D:\MARKET_DATA）
    output_dir: str = DEFAULT_MARKET_DATA_DIR
    # 是否落盘（False 时仅走总线广播）
    persist: bool = True

    # ZeroMQ 发布端口
    zmq_port: int = 5555

    # 分片写入间隔（秒）：每隔此时间将内存缓冲区写为一个完整 Parquet 分片
    flush_interval_secs: int = 30

    # Wind wsq 单批代码上限（7字段时 80×7=560 < 600 数据点限制）
    batch_size: int = 80

    # 日终合并触发时间（交易结束后自动合并当日所有分片）
    merge_hour: int = 15
    merge_minute: int = 10

    # 内存 tick 队列最大容量（防止 Wind 回调过快导致内存溢出）
    queue_maxsize: int = 200_000

    # 合约到期天数上限（记录所有 365 天内到期的合约，即全部上市合约）
    max_expiry_days: int = 365


def get_default_config() -> TradingConfig:
    """获取默认配置实例"""
    return TradingConfig()


def get_recorder_config() -> RecorderConfig:
    """获取数据记录进程默认配置实例"""
    return RecorderConfig()
