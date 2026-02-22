from __future__ import annotations

import json
import re
from typing import Any


def extract_session_ids(obj: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == "session_id" and isinstance(v, str) and v.startswith("session_"):
                found.add(v)
            found.update(extract_session_ids(v))
    elif isinstance(obj, list):
        for it in obj:
            found.update(extract_session_ids(it))
    elif isinstance(obj, str):
        for sid in re.findall(r"session_[A-Za-z0-9_]+", obj):
            found.add(sid)
    return found


def safe_json_dumps(obj: Any, *, indent: int = 2) -> str:
    try:
        return json.dumps(obj, ensure_ascii=False, indent=indent)
    except (TypeError, ValueError, OverflowError, RecursionError):
        return json.dumps({"error": "json dump failed"}, ensure_ascii=False, indent=indent)
