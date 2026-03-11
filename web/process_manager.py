from __future__ import annotations

import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

import psutil

from config.settings import DEFAULT_MARKET_DATA_DIR, DEFAULT_MIN_PROFIT, DEFAULT_EXPIRY_DAYS, DEFAULT_N_EACH_SIDE, DEFAULT_REFRESH_SECS

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def cmdline_str(proc: psutil.Process) -> str:
    try:
        return " ".join(proc.cmdline())
    except (psutil.AccessDenied, psutil.NoSuchProcess):
        return ""


def _is_real_databus_proc(proc: psutil.Process) -> bool:
    tokens = [t.lower() for t in safe_cmdline(proc)]
    if not tokens:
        return False
    if "-m" in tokens:
        try:
            mod = tokens[tokens.index("-m") + 1]
            if mod in {"data_bus.bus", "data_recorder.recorder"}:
                return True
        except Exception:
            pass
    # 兼容直接脚本启动：python data_bus/bus.py ...
    for t in tokens:
        norm = t.replace("\\", "/")
        if norm.endswith("/data_bus/bus.py") or norm.endswith("/data_recorder/recorder.py"):
            return True
    return False


def _is_real_monitor_proc(proc: psutil.Process) -> bool:
    tokens = [t.lower() for t in safe_cmdline(proc)]
    if not tokens:
        return False
    if "-m" in tokens:
        try:
            mod = tokens[tokens.index("-m") + 1]
            if mod == "monitors.monitor":
                return True
        except Exception:
            pass
    # Also support direct script invocation: python monitors/monitor.py ...
    for t in tokens:
        norm = t.replace("\\", "/")
        if norm.endswith("/monitors/monitor.py"):
            return True
    return False


def find_recorder_processes() -> List[psutil.Process]:
    result = []
    for proc in psutil.process_iter(["pid", "cmdline", "create_time"]):
        if _is_real_databus_proc(proc):
            result.append(proc)
    return result


def find_monitor_processes() -> List[psutil.Process]:
    result = []
    for proc in psutil.process_iter(["pid", "cmdline", "create_time"]):
        if _is_real_monitor_proc(proc):
            result.append(proc)
    return result


def find_infinitrader_processes() -> List[psutil.Process]:
    """
    查找 InfiniTrader 相关进程。

    说明：
    - 不同安装版本进程名可能略有差异（infinitrader/infinitrade）。
    - 这里同时匹配进程名与命令行，提高兼容性。
    """
    keys = ("infinitrader", "infinitrade")
    result: List[psutil.Process] = []
    for proc in psutil.process_iter(["pid", "name", "cmdline", "create_time"]):
        try:
            name = (proc.info.get("name") or "").lower()
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            name = ""
        cmd = cmdline_str(proc).lower()
        hay = f"{name} {cmd}"
        if any(k in hay for k in keys):
            result.append(proc)
    return result


def safe_cmdline(proc: psutil.Process) -> List[str]:
    try:
        return proc.cmdline()
    except (psutil.AccessDenied, psutil.NoSuchProcess):
        return []


def arg_from_cmd(cmdline: List[str], flag: str, default: str = "") -> str:
    for idx, token in enumerate(cmdline):
        if token == flag and idx + 1 < len(cmdline):
            return cmdline[idx + 1]
    return default


def uptime_human(proc: psutil.Process) -> str:
    try:
        sec = int(max(datetime.now().timestamp() - proc.create_time(), 0))
    except Exception:
        return "-"
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    if h > 0:
        return f"{h}h{m:02d}m"
    if m > 0:
        return f"{m}m{s:02d}s"
    return f"{s}s"


def process_info(proc: psutil.Process, kind: str) -> Dict[str, Any]:
    cmd = safe_cmdline(proc)
    params = ""
    restart_args: List[str] = []
    if kind == "databus":
        src = arg_from_cmd(cmd, "--source", "wind")
        persist = "off" if "--no-persist" in cmd else "on"
        params = (
            f"source={src} "
            f"port={arg_from_cmd(cmd, '--port', '5555')} "
            f"flush={arg_from_cmd(cmd, '--flush', '30')} "
            f"persist={persist}"
        )
    elif kind == "monitor":
        mp = arg_from_cmd(cmd, "--min-profit", str(DEFAULT_MIN_PROFIT))
        ed = arg_from_cmd(cmd, "--expiry-days", str(DEFAULT_EXPIRY_DAYS))
        nes = arg_from_cmd(cmd, "--n-each-side", str(DEFAULT_N_EACH_SIDE))
        params = f"source=zmq profit≥{mp} expiry≤{ed}d ATM上下各{nes}组"
        restart_args = [
            "--min-profit",
            mp,
            "--expiry-days",
            ed,
            "--refresh",
            arg_from_cmd(cmd, "--refresh", str(DEFAULT_REFRESH_SECS)),
            "--n-each-side",
            nes,
            "--zmq-port",
            arg_from_cmd(cmd, "--zmq-port", "5555"),
            "--snapshot-dir",
            arg_from_cmd(cmd, "--snapshot-dir", DEFAULT_MARKET_DATA_DIR),
        ]
    return {
        "pid": proc.pid,
        "kind": kind,
        "uptime": uptime_human(proc),
        "params": params,
        "restart_args": restart_args,
    }


def spawn_module(module: str, args: List[str]) -> Dict[str, Any]:
    cmd = [sys.executable, "-m", module] + args
    flags = subprocess.CREATE_NEW_CONSOLE if sys.platform == "win32" else 0
    proc = subprocess.Popen(cmd, cwd=str(PROJECT_ROOT), creationflags=flags)
    return {"pid": proc.pid}


def spawn_module_captured(module: str, args: List[str]) -> subprocess.Popen:
    """启动子进程并捕获 stdout+stderr（不弹新控制台窗口）。"""
    cmd = [sys.executable, "-u", "-m", module] + args
    env = {**__import__("os").environ, "PYTHONUNBUFFERED": "1"}
    return subprocess.Popen(
        cmd, cwd=str(PROJECT_ROOT),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1, encoding="utf-8", errors="replace",
        env=env,
    )

