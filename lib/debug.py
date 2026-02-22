from __future__ import annotations

import json
import os
import threading
import time
from typing import Any, Dict, Optional

_LOCK = threading.Lock()
_ENABLED_OVERRIDE: Optional[bool] = None
_PATH_OVERRIDE: Optional[str] = None
_MAX_BYTES_OVERRIDE: Optional[int] = None


def debug_enabled() -> bool:
    if _ENABLED_OVERRIDE is not None:
        return bool(_ENABLED_OVERRIDE)
    return False


def debug_log_path() -> str:
    if _PATH_OVERRIDE:
        return _PATH_OVERRIDE
    return "./debug.log.jsonl"


def set_debug_enabled(enabled: Optional[bool]) -> None:
    global _ENABLED_OVERRIDE
    _ENABLED_OVERRIDE = enabled


def set_debug_log_path(path: Optional[str]) -> None:
    global _PATH_OVERRIDE
    _PATH_OVERRIDE = (path or "").strip() or None


def set_debug_max_bytes(max_bytes: Optional[int]) -> None:
    global _MAX_BYTES_OVERRIDE
    if max_bytes is None:
        _MAX_BYTES_OVERRIDE = None
        return
    try:
        _MAX_BYTES_OVERRIDE = int(max_bytes)
    except (TypeError, ValueError):
        _MAX_BYTES_OVERRIDE = None


def debug_max_bytes() -> int:
    if _MAX_BYTES_OVERRIDE is not None:
        return int(_MAX_BYTES_OVERRIDE)
    return 0


def debug_log(event: str, data: Optional[Dict[str, Any]] = None) -> None:
    if not debug_enabled():
        return
    rec: Dict[str, Any] = {
        "ts": int(time.time() * 1000),
        "event": str(event or "").strip() or "event",
        "data": data or {},
    }
    try:
        line = json.dumps(rec, ensure_ascii=False)
    except (TypeError, ValueError):
        return

    path = debug_log_path()
    try:
        with _LOCK:
            try:
                max_bytes = debug_max_bytes()
                if max_bytes > 0 and os.path.exists(path):
                    if os.path.getsize(path) > max_bytes:
                        ts = int(time.time())
                        rot = path + f".{ts}.bak"
                        try:
                            os.replace(path, rot)
                        except OSError:
                            pass
            except (OSError, ValueError, TypeError):
                pass
            with open(path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
    except OSError:
        return


def _read_tail_lines(path: str, max_lines: int = 200) -> list[str]:
    try:
        max_lines = int(max_lines)
    except (TypeError, ValueError):
        max_lines = 200
    if max_lines <= 0:
        return []
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            if size <= 0:
                return []
            buf = b""
            chunk = 8192
            pos = size
            nl = 0
            while pos > 0 and nl <= (max_lines + 2):
                step = chunk if pos >= chunk else pos
                pos -= step
                f.seek(pos, os.SEEK_SET)
                part = f.read(step)
                buf = part + buf
                nl = buf.count(b"\n")
                if pos == 0:
                    break
            lines = buf.splitlines()[-max_lines:]
            return [ln.decode("utf-8", errors="replace") for ln in lines]
    except OSError:
        return []


def read_debug_events_tail(max_events: int = 50, path: Optional[str] = None) -> list[Dict[str, Any]]:
    p = (path or debug_log_path()).strip()
    lines = _read_tail_lines(p, max_lines=max_events * 3)
    out: list[Dict[str, Any]] = []
    for ln in reversed(lines):
        ln = (ln or "").strip()
        if not ln:
            continue
        try:
            rec = json.loads(ln)
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
        if isinstance(rec, dict):
            out.append(rec)
            if len(out) >= max_events:
                break
    out.reverse()
    return out


def read_debug_trace(trace_id: str, max_events: int = 200, path: Optional[str] = None) -> list[Dict[str, Any]]:
    tid = (trace_id or "").strip()
    if not tid:
        return []
    p = (path or debug_log_path()).strip()
    out: list[Dict[str, Any]] = []
    try:
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            for ln in f:
                ln = (ln or "").strip()
                if not ln:
                    continue
                try:
                    rec = json.loads(ln)
                except (json.JSONDecodeError, TypeError, ValueError):
                    continue
                if not isinstance(rec, dict):
                    continue
                data = rec.get("data")
                if isinstance(data, dict) and str(data.get("trace_id", "") or "").strip() == tid:
                    out.append(rec)
                    if len(out) >= max_events:
                        break
    except OSError:
        return []
    return out
