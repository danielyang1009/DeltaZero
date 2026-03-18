from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, Optional

from config.settings import ETF_CODE_TO_NAME
from utils.time_utils import bj_from_timestamp, bj_now_naive, bj_today


def read_snapshot_stats(market_data_dir: str) -> Optional[Dict]:
    snap_path = Path(market_data_dir) / "snapshot_latest.parquet"
    if not snap_path.exists():
        return None
    try:
        import pandas as pd

        df = pd.read_parquet(str(snap_path))
        if "type" in df.columns:
            opts = df[df["type"] == "option"]
            etfs = df[df["type"] == "etf"]
        else:
            opts = df
            etfs = pd.DataFrame()
        underlying_counts: Dict[str, int] = {}
        if "underlying" in df.columns:
            for code, name in ETF_CODE_TO_NAME.items():
                cnt = int((df["underlying"] == code).sum())
                if cnt > 0:
                    underlying_counts[name] = cnt
        adj_count = int(df["is_adjusted"].sum()) if "is_adjusted" in df.columns else 0
        mtime = bj_from_timestamp(snap_path.stat().st_mtime).replace(tzinfo=None)
        return {
            "n_options": len(opts),
            "n_etf": len(etfs),
            "underlying_counts": underlying_counts,
            "adj_count": adj_count,
            "mtime": mtime,
        }
    except Exception as exc:
        return {"error": str(exc)}


def count_today_chunks(market_data_dir: str) -> Dict:
    chunks_dir = Path(market_data_dir) / "chunks"
    today_str = bj_today().strftime("%Y%m%d")
    if not chunks_dir.exists():
        return {"n_opt": 0, "n_etf": 0, "total_mb": 0.0, "latest_time": None}
    opt_chunks = sorted(chunks_dir.glob(f"options_{today_str}_*.parquet"))
    etf_chunks = sorted(chunks_dir.glob(f"etf_{today_str}_*.parquet"))
    all_chunks = opt_chunks + etf_chunks
    total_bytes = sum(c.stat().st_size for c in all_chunks if c.exists())
    latest_time: Optional[datetime] = None
    if all_chunks:
        latest_time = bj_from_timestamp(max(c.stat().st_mtime for c in all_chunks if c.exists())).replace(tzinfo=None)
    return {
        "n_opt": len(opt_chunks),
        "n_etf": len(etf_chunks),
        "total_mb": total_bytes / (1024 * 1024),
        "latest_time": latest_time,
    }


def run_merge(target_date: date, market_data_dir: str) -> Dict[str, Any]:
    d_str = target_date.strftime("%Y%m%d")
    chunks_dir = Path(market_data_dir) / "chunks"
    opt_chunks = sorted(chunks_dir.glob(f"options_{d_str}_*.parquet")) if chunks_dir.exists() else []
    etf_chunks = sorted(chunks_dir.glob(f"etf_{d_str}_*.parquet")) if chunks_dir.exists() else []
    if not opt_chunks and not etf_chunks:
        return {"ok": True, "output": f"未找到 {target_date} 的分片文件，无需合并"}
    try:
        from data_bus.parquet_writer import ParquetWriter

        writer = ParquetWriter(market_data_dir)
        writer.merge_daily(target_date)
    except Exception as exc:
        return {"ok": False, "output": f"合并失败: {exc}"}
    lines = []
    for prefix in ("options", "etf"):
        out = Path(market_data_dir) / f"{prefix}_{d_str}.parquet"
        if out.exists():
            size_mb = out.stat().st_size / (1024 * 1024)
            lines.append(f"✓ {out.name}  ({size_mb:.1f} MB)")
    lines.append("合并完成")
    return {"ok": True, "output": "\n".join(lines)}


def fmt_time_short(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    return dt.strftime("%H:%M:%S")


def age_str(dt: datetime) -> str:
    sec = int((bj_now_naive() - dt).total_seconds())
    if sec < 60:
        return f"{sec}s前"
    if sec < 3600:
        return f"{sec // 60}min前"
    return f"{sec // 3600}h前"


def snapshot_readable(raw: Optional[Dict]) -> Dict[str, Any]:
    if raw is None:
        return {"status": "not_found", "text": "快照文件不存在（DataBus 尚未写入）"}
    if "error" in raw:
        return {"status": "error", "text": raw["error"]}
    uc = raw.get("underlying_counts", {})
    parts = [f"{name} {cnt}合约" for name, cnt in uc.items()]
    coverage = "  ".join(parts) if parts else "-"
    mtime: Optional[datetime] = raw.get("mtime")
    return {
        "status": "ok",
        "coverage": coverage,
        "n_options": raw.get("n_options", 0),
        "adj_count": raw.get("adj_count", 0),
        "n_etf": raw.get("n_etf", 0),
        "mtime": fmt_time_short(mtime),
        "mtime_age": age_str(mtime) if mtime else None,
    }


def chunks_readable(raw: Dict) -> Dict[str, Any]:
    lt = raw.get("latest_time")
    return {
        "n_opt": raw.get("n_opt", 0),
        "n_etf": raw.get("n_etf", 0),
        "total_mb": round(raw.get("total_mb", 0), 2),
        "latest_time": fmt_time_short(lt) if isinstance(lt, datetime) else lt,
    }


def merge_status_readable(market_data_dir: str, target_date: Optional[date] = None) -> Dict[str, Any]:
    """
    返回今日（日）合并状态。
    判定依据：是否生成 options_YYYYMMDD.parquet / etf_YYYYMMDD.parquet。
    """
    d = target_date or bj_today()
    d_str = d.strftime("%Y%m%d")
    root = Path(market_data_dir)

    # 兼容两种布局：根目录直接放 / 品种子目录分放
    opt_files = list(root.glob(f"options_{d_str}.parquet")) + list(root.glob(f"*/options_{d_str}.parquet"))
    etf_files = list(root.glob(f"etf_{d_str}.parquet")) + list(root.glob(f"*/etf_{d_str}.parquet"))

    opt_ok = len(opt_files) > 0
    etf_ok = len(etf_files) > 0
    merged_any = opt_ok or etf_ok
    merged_full = opt_ok and etf_ok

    latest_mtime: Optional[datetime] = None
    if merged_any:
        mtimes = [f.stat().st_mtime for f in opt_files + etf_files]
        if mtimes:
            latest_mtime = bj_from_timestamp(max(mtimes)).replace(tzinfo=None)

    return {
        "date": d_str,
        "merged_any": merged_any,
        "merged_full": merged_full,
        "options_exists": opt_ok,
        "etf_exists": etf_ok,
        "mtime": fmt_time_short(latest_mtime) if latest_mtime else None,
        "mtime_age": age_str(latest_mtime) if latest_mtime else None,
    }

