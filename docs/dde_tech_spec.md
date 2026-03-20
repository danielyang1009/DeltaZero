# DDE 技术说明

## 什么是 DDEML ADVISE 模式

**DDE（Dynamic Data Exchange）** 是 Windows 1987 年引入的 IPC 机制，至今仍被国内行情软件（通达信/QD 等）用于对外暴露实时行情。

Windows 提供两套 DDE API：
- **原始 DDE（WM_DDE_\*）**：直接收发 Windows 消息，极难用
- **DDEML**（DDE Management Library）：`user32.dll` 里的高层封装，本项目使用此套

本项目用 Python `ctypes` 直调 `user32.dll` 中的 DDEML 函数，等价于 C 代码调 Win32 API。

**ADVISE 模式（热链接）**：客户端注册订阅后，服务端有更新时**主动推送**，无需轮询。区别于 REQUEST（冷链接，问一次答一次）。

## 完整数据流

```
行情软件（QD）
   │  Windows 消息总线
   ▼
_DDEClient._dde_callback()      ← DDEML 在消息泵线程触发
   │  解析 XlTable 二进制流（type=0x0001 FLOAT 记录）
   ▼
_tick_buf                        ← 价格三件套+至少一个量字段到齐后触发回调
   ▼
DDEDirectSubscriber._on_tick() → tick_queue → DataBus ZMQ PUB
```

## 关键实现细节

| 步骤 | API | 说明 |
|------|-----|------|
| 初始化 | `DdeInitializeW` | 注册为纯客户端，传入回调函数指针 |
| 建连 | `DdeConnect` | service=`"QD"`，topic=xlsx 里的不透明数字（如 `"2206355670"`） |
| 订阅 | `DdeClientTransaction(XTYP_ADVSTART)` | 每个字段（LASTPRICE 等）单独注册 |
| 消息泵 | `PeekMessageW` 循环 | DDEML 回调通过 Windows 消息队列派发，**必须在同一线程内持续运行** |

## XlTable 二进制格式

ADVISE 回调收到的数据为 XlTable 二进制流（不是字符串）：

```
偏移 0: type=0x0010 (TABLE), size=4  → 容器头，跳过其 4 字节数据体
偏移 8: type=0x0001 (FLOAT), size=8  → struct.unpack("<d") 读 IEEE 754 双精度浮点
```

正确解析：从 `off=0` 开始流式处理，`off += rsize` 跳过记录体，遇到 `type==0x0001 and size==8` 时用 `struct.unpack_from("<d", raw, off)` 读取浮点值。**若从 `off=4` 开始则跳过了 FLOAT 记录，永远取不到数据。**

## 为什么不用 pywin32

`pywin32.dde` 的 `ConnectTo()` 对 QD 服务握手方式不兼容，连接必定失败。ctypes DDEML 是唯一可靠路径。

## 为什么 topic 不能推算

topic 是行情软件内部的不透明数字字符串（如 `"2206355670"`），软件升级后可能改变，**只能从 `metadata/wxy_options.xlsx` 的 externalLink XML 中读取**，禁止用代码规则推算。

## wxy_options.xlsx 解析细节

xlsx 是 ZIP，`_load_topic_map()` 解析其中的 `xl/externalLinks/externalLink*.xml`：

| 字段 | XML 属性 | 实际值 |
|------|----------|--------|
| service | `ddeService` | `"QD"`（不是 `"TdxW"`，写错则全部连接失败） |
| topic | `ddeTopic` | 每个合约对应一个不透明数字（如 `"2206355670"`） |
| item | 列名 | `LASTPRICE`、`BIDPRICE1`、`ASKPRICE1`、`BIDVOLUME1`、`ASKVOLUME1` |

`_load_topic_map()` 在 DataBus 启动时读取一次，返回 `(code→topic dict, service_name)`。

## 禁止事项

- **禁止用 `pywin32 dde` 模块替换**：`ConnectTo()` 对 QD 服务连接必定失败，只有 ctypes DDEML 可用
- **禁止将 DDE 操作改为异步或多线程并发**：`DdeConnect` 依赖 Windows 消息泵，必须在单一线程内串行调用并在每次 connect 后立即 `_pump_messages()`
- **禁止从代码推算 service/topic**：所有地址信息来自 xlsx，软件升级后地址可能改变

## DDE 测试流程

1. 确认 `metadata/wxy_options.xlsx` 已放入 `metadata/`
2. 启动 DataBus：`python -m data_bus.bus --source dde`
3. 30 秒后查看自检日志：`DDE 自检(30s): 累计=N tick, 期权标的=[...]`
