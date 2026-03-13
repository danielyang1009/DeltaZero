# DDE 脱离 Excel 直连——可行性研究

## 背景

当前架构依赖 Excel 打开 `wxy_options.xlsx` 来"激活"交易软件的 DDE 服务端。
本文记录脱离 Excel 的几种可行方向，供未来评估和实施。

---

## 首先要确认的问题

在选择方案之前，需要先弄清楚失败的根本原因。不开 Excel 直接启动 DataBus，
看日志中 `DDE 连接: X 成功 / Y 失败` 的结果：

| 现象 | 含义 | 对应方案 |
|------|------|----------|
| `hConv = NULL`，`err = 0x400a`（DMLERR_NO_CONV_ESTABLISHED） | 交易软件直接拒绝连接，DDE 服务端未注册 | 方案一、二 |
| `hConv` 非 NULL，但 ADVISE 回调从不触发 | 连上了但不推数据，topic 未激活 | 方案三 |

---

## 方案一：`win32com` 无界面驱动 Excel（最可靠）

本质上仍然用 Excel，但通过 COM 自动化，省去手动操作，可集成进 DataBus 启动流程。

```python
import win32com.client

def activate_dde_via_excel(xlsx_path: str) -> object:
    excel = win32com.client.Dispatch("Excel.Application")
    excel.Visible = False
    excel.DisplayAlerts = False
    wb = excel.Workbooks.Open(xlsx_path)
    return excel   # 保持引用，防止 Excel 被 GC 关闭

# DataBus 启动时调用，持有 excel 对象直到 stop()
```

**优点**：最稳定，行为与手动操作完全一致。
**缺点**：仍依赖 Excel 安装；Excel 进程常驻内存（约 100~200MB）。

---

## 方案二：直接发 `WM_DDE_INITIATE` 广播（绕过 Excel）

`DdeConnect` 内部就是向所有顶层窗口广播 `WM_DDE_INITIATE`。
我们的 ctypes DDEML 代码已经在做这件事——如果 `DdeConnect` 返回 NULL，
可以在此之前先手动广播，看交易软件是否响应：

```python
# 伪代码：手动广播 WM_DDE_INITIATE
WM_DDE_INITIATE = 0x03E0
HWND_BROADCAST  = 0xFFFF

atom_service = GlobalAddAtom("QD")
atom_topic   = GlobalAddAtom("2206355670")
SendMessage(HWND_BROADCAST, WM_DDE_INITIATE, hwnd_self,
            MAKELPARAM(atom_service, atom_topic))
```

**优点**：不依赖 Excel，纯 Windows API。
**缺点**：`DdeConnect` 已经做了这件事；若交易软件刻意过滤非 Excel 客户端，此方案无效。

---

## 方案三：先发 `XTYP_REQUEST` 触发 topic 懒加载

部分 DDE 服务端对 topic 做懒加载，需要先收到一次 REQUEST 才会激活该 topic 的 ADVISE。
在现有 `_connect_and_advise()` 里，建立 hConv 后、发 ADVSTART 前，先发一次 REQUEST：

```python
# 在 _connect_and_advise() 中，每个 hConv 建立后插入：
result_handle = u32.DdeClientTransaction(
    None, 0, hConv, item_hsz, CF_TEXT,
    XTYP_REQUEST,    # 0x20B0
    5000, None,
)
if result_handle:
    u32.DdeFreeDataHandle(result_handle)
# 再执行原有的 XTYP_ADVSTART
```

**优点**：改动最小，只需修改 `dde_direct_client.py` 几行。
**缺点**：不能解决 `DdeConnect` 本身失败的情况。

---

## 方案四：监控交易软件窗口，等其就绪再连接

交易软件启动时会创建特定标题的顶层窗口。可以用 `FindWindow` / `EnumWindows`
检测交易软件是否就绪，再发起 DDE 连接，避免过早连接导致失败后不重试：

```python
import ctypes

def find_qd_window() -> bool:
    """检测通达信主窗口是否存在。"""
    hwnd = ctypes.windll.user32.FindWindowW(None, "通达信")   # 窗口标题需实测
    return hwnd != 0

# DataBus 启动时轮询，找到窗口后再调 _connect_and_advise()
```

**优点**：可以和其他方案组合使用，防止启动顺序问题。
**缺点**：窗口标题需要实测确认，软件版本更新可能变化。

---

## 优先建议

1. **先做诊断**：不开 Excel，直接 `python -m data_bus.bus --source dde`，看日志中连接成功/失败数量。
2. 若 `DdeConnect` 全部失败 → 优先尝试**方案一**（win32com，改动最小，最稳定）。
3. 若 `DdeConnect` 成功但无数据 → 尝试**方案三**（加 XTYP_REQUEST，改动最小）。
4. 长期目标：方案一作为过渡，方案二（纯 Windows API）作为最终去 Excel 方向。
