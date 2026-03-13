# DDE 数据流全链路说明

## 概览

```
交易软件（QD进程）
    ↓ DDE ADVISE（Windows消息，每字段一条）
_DDEClient._dde_callback（消息泵线程）
    ↓ 攒够价格+量字段
_accumulate → on_tick_cb
    ↓
DDEDirectSubscriber._on_tick → TickPacket → Queue
    ↓
data_bus/bus.py 主循环
    ├── 写 Parquet（D:\MARKET_DATA\chunks\）
    └── ZMQ PUB tcp://127.0.0.1:5555
             ↓
    monitors / market_cache ZMQ SUB
```

---

## 阶段一：寻址——从 xlsx 找到 DDE 地址

**相关代码**：`data_bus/dde_direct_client.py` — `_load_topic_map()`、`_parse_xlsx_topic_map()`

`wxy_options.xlsx` 是交易软件（通达信/QD）导出的 Excel 文件，本质是个 ZIP。
解压后 `xl/externalLinks/externalLink*.xml` 存有所有 DDE 连接参数：

| 参数 | XML 属性 | 示例值 | 说明 |
|------|----------|--------|------|
| service | `ddeService` | `"QD"` | DDE 服务名，固定死在软件里，写错则全部连接失败 |
| topic | `ddeTopic` | `"2206355670"` | 每个合约的不透明 ID，**不可推算**，软件升级后可能改变 |
| item | 工作表公式列 | `LASTPRICE` 等 | 字段名，见下表 |

**字段映射**（`_FIELD_MAP`）：

| 内部字段 | DDE item 名 |
|----------|-------------|
| `last`  | `LASTPRICE`  |
| `bid1`  | `BIDPRICE1`  |
| `ask1`  | `ASKPRICE1`  |
| `bidv1` | `BIDVOLUME1` |
| `askv1` | `ASKVOLUME1` |

`_load_topic_map()` 在 DataBus 启动时**解析一次**，得到 `{合约代码 → topic}` 映射表。
优先读 `metadata/wxy_options.xlsx`（合并文件），不存在则分别读三个品种文件。

---

## DDE 的数据在哪里？

DDE（Dynamic Data Exchange）是 **Windows 操作系统级别的进程间通信机制**，走的是 Windows 消息队列，**不是共享内存，也不是我们主动去读取**。

具体过程：

1. 交易软件（QD进程）持有行情数据在**它自己的进程内存**里，我们无法直接访问
2. 我们调 `DdeConnect` 与它建立连接，注册 `XTYP_ADVSTART`（订阅通知）
3. 交易软件报价变动时，**主动通过 Windows 消息机制**（`XTYP_ADVDATA`）把数据推过来
4. 我们的消息泵线程（`PeekMessage → DispatchMessage`）接收这条消息，触发 `_dde_callback`
5. 在回调里用 `DdeAccessData` 从 **DDE 数据句柄**（`HDDEDATA`）读出字节——这是 Windows 在消息传递时拷贝过来的副本

整个过程是**推模式**（交易软件主动推），不是我们去轮询或读内存。
类比：两个进程通过操作系统的消息信箱通信，而不是直接互相看对方的内存。

---

## 阶段二：建立 DDE 连接（ctypes DDEML）

**相关代码**：`_DDEClient._message_loop()`、`_connect_and_advise()`

DDE 是古老的 Windows 进程间通信机制，只能由 Windows 消息泵驱动。
`_DDEClient` 在专用线程（`dde-direct-pump`）里串行执行以下操作：

1. **`DdeInitializeW`** — 注册 DDEML 实例，交出回调函数指针（`_APPCMD_CLIENTONLY` 模式）
2. 对每个 topic，调 **`DdeConnect(idInst, service="QD", topic="...")`** — 连接到交易软件进程
3. 每次 `DdeConnect` 后立即 **`_pump_messages()`**（处理 `WM_DDE_ACK`），连接握手才能完成
4. 对每个连接，为五个字段各发一次 **`XTYP_ADVSTART`** — 告诉交易软件"数据变化时主动推送"

> **禁止并发**：`DdeConnect` 依赖消息泵，所有 DDE 操作必须在同一线程内串行执行，
> 每次 connect 后必须立即 `_pump_messages()`，否则握手失败。

连接建立后，线程进入持续的消息泵循环（`PeekMessage → TranslateMessage → DispatchMessage`，5ms 间隔），等待交易软件推送行情。

---

## 阶段三：接收数据（XTYP_ADVDATA 回调）

**相关代码**：`_DDEClient._dde_callback()`、`_read_advise_data()`、`_dde_parse_response()`

交易软件报价变动时，主动触发 `_dde_callback`（在消息泵线程中执行）：

```
XTYP_ADVDATA 回调
  → DdeQueryStringW(hsz1) → topic 字符串 → 查反向映射 → 合约代码
  → DdeQueryStringW(hsz2) → item 名（如 "LASTPRICE"） → 查 _ITEM_TO_FIELD → 字段名（"last"）
  → DdeAccessData(hdata)  → 原始字节
  → _dde_parse_response() → float 值
  → _accumulate(code, field, value)
  → 返回 _DDE_FACK（告知交易软件已收到）
```

### XlTable 二进制解析

交易软件返回的**不是文本**，而是 XlTable 二进制流。格式为连续的 `(type:u16, size:u16, data[size])` 记录：

| type 值 | 含义 | 处理方式 |
|---------|------|----------|
| `0x0010` (TABLE) | 外层容器头 | 跳过其 4 字节数据体，继续读 |
| `0x0001` (FLOAT) + size=8 | IEEE 754 double | `struct.unpack_from("<d", raw, off)` 读取 |
| `0x0006` (INT) + size=2 | 16位整数 | `struct.unpack_from("<h", raw, off)` 读取 |
| `0x0005` (BLANK) / `0x0004` (ERROR) | 无效值 | 返回 None |

> **关键**：必须从 `off=0` 开始流式处理。若从 `off=4` 开始则会跳过 FLOAT 记录，永远取不到数据。

文本格式（GBK 编码）作为兜底，处理非 XlTable 响应。

---

## 阶段四：凑字段触发 tick

**相关代码**：`_DDEClient._accumulate()`

DDE 是**逐字段推送**的，每次回调只来一个字段。用 `_tick_buf` 缓冲，攒够后才触发：

```python
_tick_buf[code][field] = value

# 触发条件
if {last, bid1, ask1} ⊆ buf.keys()       # 三个价格字段全到
   and buf.keys() & {bidv1, askv1}:        # 至少一个量字段到达
    on_tick_cb(code, dict(buf), ts_ms)
    buf.clear()   # 清空，等下一批
```

触发条件要求量字段到达，是为了防止量字段未到时就发出全零 volume 的 tick。

---

## 阶段五：进入数据总线

**相关代码**：`DDEDirectSubscriber._on_tick()`、`data_bus/bus.py`

`on_tick_cb` 即 `DDEDirectSubscriber._on_tick`，根据代码类型分发：

```
_on_tick(code, fields, ts_ms)
    ├── code 是 ETF？ → _emit_etf_tick  → TickPacket(is_etf=True,  tick_obj=ETFTickData)
    └── code 是期权？ → _emit_option_tick → TickPacket(is_etf=False, tick_obj=TickData)
                            → queue.put_nowait(pkt)
```

`queue` 是 `data_bus/bus.py` 传入的 Python `Queue`。DataBus 主循环从 queue 取出 `TickPacket`：

- **落盘**：写 Parquet 分片到 `D:\MARKET_DATA\chunks\`，每 30s 刷盘，15:10 触发日终合并
- **广播**：`ZMQPublisher` 以 `OPT_` / `ETF_` 前缀通过 `tcp://127.0.0.1:5555` PUB 出去

---

## 阶段六：下游消费

ZMQ SUB 订阅者各自独立消费：

| 消费者 | 线程 | 处理方式 |
|--------|------|----------|
| `monitors/monitor.py` | 主进程 | 无 CONFLATE，批量接收，增量更新 aligner，Rich 终端渲染 |
| `web/market_cache.py` market-cache-zmq | Thread-1 | CONFLATE=1，只保最新，写 `_lkv` LKV 快照 |
| `web/market_cache.py` market-cache-monitor | Thread-3 | 无 CONFLATE，增量更新 aligner，PCP 套利计算推送 WS |

---

## 关键约束总结

| 约束 | 原因 |
|------|------|
| 禁止用 `pywin32 dde` 模块 | `ConnectTo()` 对 QD 服务必定失败，只有 ctypes DDEML 可用 |
| DDE 操作必须在单一线程串行 | `DdeConnect` 依赖 Windows 消息泵，跨线程调用会死锁/失败 |
| service/topic 只能来自 xlsx | 无法从代码推算，QD 的 topic 是不透明数字，软件升级后可能改变 |
| connect 后必须立即 pump | DDE 握手靠 `WM_DDE_ACK` 消息完成，不 pump 则 hConv 为 NULL |
| 量字段到达才 emit tick | 防止 volume 字段落后于价格字段，导致发出全零量的 tick |
