"""请求日志：JSON Lines 按日分文件，异步写入，按天数自动清理。

存储：data/logs/requests-YYYY-MM-DD.jsonl，一行一条 JSON。
字段截断防止超长消息撑爆：user_msg 100 字、fake_reply 200 字。
"""
from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from . import config

_LOG_DIR = config.CONFIG_PATH.parent / "logs"
_write_lock = asyncio.Lock()
_file_handles: dict[str, Any] = {}  # date_str -> file handle


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _log_path(date_str: str) -> Path:
    return _LOG_DIR / f"requests-{date_str}.jsonl"


def _get_handle(date_str: str):
    if date_str in _file_handles:
        return _file_handles[date_str]
    _LOG_DIR.mkdir(parents=True, exist_ok=True)
    fh = open(_log_path(date_str), "a", encoding="utf-8")
    _file_handles[date_str] = fh
    # 关掉非今天的句柄
    for k in list(_file_handles):
        if k != date_str:
            try:
                _file_handles[k].close()
            except Exception:
                pass
            del _file_handles[k]
    return fh


def _truncate(text: str, limit: int) -> str:
    if not text:
        return ""
    return text[:limit] + "…" if len(text) > limit else text


async def write(entry: dict[str, Any]) -> None:
    """异步写一条日志。entry 里会自动补 ts。"""
    settings = config.get()
    if not settings.log_enabled:
        return
    entry["ts"] = int(time.time())
    # 截断长字段
    if "user_msg_snippet" in entry:
        entry["user_msg_snippet"] = _truncate(str(entry["user_msg_snippet"]), 100)
    if "fake_reply_snippet" in entry:
        entry["fake_reply_snippet"] = _truncate(str(entry["fake_reply_snippet"]), 200)

    date_str = _today()
    line = json.dumps(entry, ensure_ascii=False) + "\n"

    async with _write_lock:
        try:
            fh = _get_handle(date_str)
            fh.write(line)
            fh.flush()
        except Exception:
            pass  # 日志写失败不影响主流程


async def read(
    date: str = "",
    branch: str = "",
    rule: str = "",
    page: int = 1,
    size: int = 50,
) -> dict[str, Any]:
    """读日志，支持过滤和分页。返回 {total, page, size, items}。"""
    if not date:
        date = _today()
    path = _log_path(date)
    if not path.exists():
        return {"total": 0, "page": page, "size": size, "items": []}

    items: list[dict] = []
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                if branch and obj.get("branch") != branch:
                    continue
                if rule and rule.lower() not in (obj.get("rule_id") or "").lower() \
                        and rule.lower() not in (obj.get("rule_name") or "").lower():
                    continue
                items.append(obj)
    except Exception:
        return {"total": 0, "page": page, "size": size, "items": []}

    # 倒序：最新在前
    items.reverse()
    total = len(items)
    start = (page - 1) * size
    end = start + size
    return {"total": total, "page": page, "size": size, "items": items[start:end]}


async def list_dates() -> list[str]:
    """列出所有有日志的日期，倒序。"""
    if not _LOG_DIR.exists():
        return []
    dates = []
    for p in _LOG_DIR.glob("requests-*.jsonl"):
        d = p.stem.replace("requests-", "")
        dates.append(d)
    return sorted(dates, reverse=True)


async def clear_all() -> int:
    """删除所有日志文件，返回删除数量。"""
    global _file_handles
    for fh in _file_handles.values():
        try:
            fh.close()
        except Exception:
            pass
    _file_handles = {}
    count = 0
    if _LOG_DIR.exists():
        for p in _LOG_DIR.glob("requests-*.jsonl"):
            try:
                p.unlink()
                count += 1
            except Exception:
                pass
    return count


async def cleanup_old() -> int:
    """删除超过保留天数的日志文件，返回删除数量。"""
    settings = config.get()
    retention = settings.log_retention_days
    if retention < 0:
        return 0
    cutoff = (datetime.now() - timedelta(days=retention)).strftime("%Y-%m-%d")
    count = 0
    if _LOG_DIR.exists():
        for p in _LOG_DIR.glob("requests-*.jsonl"):
            d = p.stem.replace("requests-", "")
            if d < cutoff:
                try:
                    p.unlink()
                    count += 1
                except Exception:
                    pass
    return count


async def cleanup_loop() -> None:
    """后台任务：每小时检查一次，删除过期日志。"""
    while True:
        await asyncio.sleep(3600)
        try:
            await cleanup_old()
        except Exception:
            pass


def start_cleanup_task() -> None:
    """在应用启动时调用，启动后台清理任务。"""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            loop.create_task(cleanup_loop())
    except Exception:
        pass
