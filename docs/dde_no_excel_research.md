# DDE 脱离 Excel 直连——可行性研究

## 背景

当前使用 DDE 前需要手动在交易软件里点击"导出 DDE"菜单，同时保持 Excel 打开
`wxy_options.xlsx`。本文分析"开门"的真实机制，并记录脱离手动操作的可行方向。

---

## 真实机制分析：谁在"开门"？

目前的操作顺序是：

```
点击交易软件"导出 DDE"菜单
    ↓ 交易软件调 DdeInitializeW（服务端模式）
    ↓ 调 DdeNameService("QD", DNS_REGISTER) → 向 Windows 广播注册
    ↓ 为当前界面合约注册 topic handler
Excel 打开 wxy_options.xlsx（含 DDE 公式）
    ↓ Excel 作为客户端 DdeConnect → 建立连接
我们的 ctypes 代码 DdeConnect → 搭便车连上
```

**核心结论**：真正"开门"的是**"导出 DDE"这个菜单操作**，而不是 Excel。
Excel 只是恰好作为接收方被打开，顺带建立了连接。

`wxy_options.xlsx` 里记录的 topic 地址，是上一次点击"导出 DDE"时生成的，
只要软件版本未升级，地址不变，文件无需重新导出。

---

## 诊断：先确认失败根本原因

不开 Excel，只点"导出 DDE"后直接启动 DataBus：

```bash
python -m data_bus.bus --source dde
```

观察日志中 `DDE 连接: X 成功 / Y 失败`：

| 现象 | 含义 | 对应方案 |
|------|------|----------|
| 成功数 > 0，数据正常推送 | 开门的确实是菜单操作，Excel 不是必需的 | 无需额外方案 |
| `hConv = NULL`，`err = 0x400a` | DDE 服务端未注册，菜单未触发或软件未就绪 | 方案一、方案二 |
| `hConv` 非 NULL 但回调不触发 | 连上了但 topic 未激活 | 方案三 |

---

## 方案一：触发交易软件"导出 DDE"菜单（最优先验证）

如果确认是菜单操作激活了 DDE 服务，可以用 Windows API 模拟这个点击，
彻底省去手动操作，比模拟 Excel 更干净。

思路是通过 `FindWindow` + `SendMessage(WM_COMMAND)` 触发交易软件的菜单项：

```python
import ctypes

def trigger_dde_export(window_title: str = "通达信") -> bool:
    """向交易软件发送"导出 DDE"菜单命令。"""
    hwnd = ctypes.windll.user32.FindWindowW(None, window_title)  # 窗口标题需实测
    if not hwnd:
        return False
    # 菜单命令 ID 需通过 Spy++ 或 Resource Hacker 实测获取
    WM_COMMAND  = 0x0111
    menu_cmd_id = 0xXXXX   # 待实测
    ctypes.windll.user32.PostMessageW(hwnd, WM_COMMAND, menu_cmd_id, 0)
    return True
```

**前置工作**：
1. 用 Spy++ 或 Resource Hacker 找到"导出 DDE"对应的菜单命令 ID
2. 确认交易软件主窗口的窗口标题（`FindWindowW` 的第二个参数）

**优点**：最干净，完全不依赖 Excel，可集成进 DataBus 启动流程。
**缺点**：需要实测菜单命令 ID，软件版本更新后 ID 可能变化。

---

## 方案二：`win32com` 无界面驱动 Excel（最稳定的过渡方案）

如果方案一的菜单 ID 难以获取，或交易软件行为不稳定，可以退而求其次：
用 COM 自动化无界面驱动 Excel 打开 xlsx，省去手动操作，但仍依赖 Excel。

```python
import win32com.client

def activate_dde_via_excel(xlsx_path: str) -> object:
    excel = win32com.client.Dispatch("Excel.Application")
    excel.Visible = False
    excel.DisplayAlerts = False
    wb = excel.Workbooks.Open(xlsx_path)
    return excel   # 必须持有引用，防止 Excel 被 GC 关闭导致 DDE 断开

# DataBus 启动时调用，stop() 时 excel.Quit()
```

**优点**：行为与手动操作完全一致，稳定可靠。
**缺点**：仍依赖 Excel 安装；Excel 进程常驻内存（约 100~200MB）。

---

## 方案三：先发 `XTYP_REQUEST` 触发 topic 懒加载

适用于"连接成功但不推数据"的情况。部分 DDE 服务端对 topic 做懒加载，
需要先收到一次 REQUEST 才激活 ADVISE。在 `_connect_and_advise()` 里，
建立 `hConv` 后、发 `XTYP_ADVSTART` 前插入一次 REQUEST：

```python
# data_bus/dde_direct_client.py — _connect_and_advise() 中，每个 hConv 建立后插入：
result_handle = u32.DdeClientTransaction(
    None, 0, hConv, item_hsz, CF_TEXT,
    XTYP_REQUEST,    # 0x20B0，一次性请求
    5000, None,
)
if result_handle:
    u32.DdeFreeDataHandle(result_handle)
# 再执行原有的 XTYP_ADVSTART
```

**优点**：改动最小，只需修改 `dde_direct_client.py` 几行。
**缺点**：不能解决 `DdeConnect` 本身失败（服务端未注册）的情况。

---

## 建议执行顺序

```
第一步：诊断
  只点"导出 DDE"，不开 Excel，直接启动 DataBus，看连接日志
       ↓
  连接成功且有数据？→ 问题已解，Excel 本就不需要
       ↓ 否
  hConv = NULL？→ 方案一（模拟菜单）→ 方案二（win32com Excel）
  hConv 非 NULL 但无数据？→ 方案三（加 XTYP_REQUEST）
```

长期目标：方案一（模拟菜单触发）作为最终形态，方案二作为过渡兜底。
