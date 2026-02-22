from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from lib.config import (
    DEBUG_LOG_PATH,
    DEFAULT_AI_BASE_URL,
    DEFAULT_AI_MODEL,
    DEFAULT_AI_TIMEOUT,
    DEFAULT_BASE_URL,
    DEFAULT_TIMEOUT,
    MAX_CONTEXT_CHARS,
    MAX_CONTEXT_MESSAGES,
    MAX_TOOL_RESULT_CHARS,
)


@dataclass(frozen=True)
class CfgField:
    key: str
    kind: str
    default: Any
    required: bool = True
    non_empty: bool = False
    min_int: Optional[int] = None


CFG_FIELDS: List[CfgField] = [
    CfgField("R2_BASE_URL", "str", DEFAULT_BASE_URL, required=True, non_empty=True),
    CfgField("AI_BASE_URL", "str", DEFAULT_AI_BASE_URL, required=True, non_empty=True),
    CfgField("AI_MODEL", "str", DEFAULT_AI_MODEL, required=True, non_empty=True),
    CfgField("AI_API_KEY", "str", "", required=True, non_empty=False),
    CfgField("AI_ENABLE_SEARCH", "bool", False, required=True),
    CfgField("AI_ENABLE_THINKING", "bool", False, required=True),
    CfgField("DEBUG_ENABLED", "bool", False, required=True),
    CfgField("DEBUG_LOG_PATH", "str", DEBUG_LOG_PATH, required=True, non_empty=True),
    CfgField("MCP_TIMEOUT_S", "int", int(DEFAULT_TIMEOUT), required=True, min_int=1),
    CfgField("AI_TIMEOUT_S", "int", int(DEFAULT_AI_TIMEOUT), required=True, min_int=1),
    CfgField("MAX_TOOL_RESULT_CHARS", "int", int(MAX_TOOL_RESULT_CHARS), required=True, min_int=200),
    CfgField("MAX_CONTEXT_MESSAGES", "int", int(MAX_CONTEXT_MESSAGES), required=True, min_int=5),
    CfgField("MAX_CONTEXT_CHARS", "int", int(MAX_CONTEXT_CHARS), required=True, min_int=2000),
    CfgField("DANGEROUS_POLICY", "str", "confirm", required=True, non_empty=True),
    CfgField("DANGEROUS_ALLOW_REGEX", "str", "", required=True, non_empty=False),
    CfgField("DANGEROUS_EXTRA_DENY_REGEX", "str", "", required=True, non_empty=False),
    CfgField("DEBUG_MAX_BYTES", "int", 0, required=True, min_int=0),
]


def _parse_bool(v: Any) -> Optional[bool]:
    if isinstance(v, bool):
        return v
    if isinstance(v, int) and v in {0, 1}:
        return bool(v)
    if isinstance(v, str):
        s = v.strip().lower()
        if s in {"1", "true", "yes", "y", "on"}:
            return True
        if s in {"0", "false", "no", "n", "off"}:
            return False
    return None


def _parse_int(v: Any) -> Optional[int]:
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, str):
        s = v.strip()
        if s.isdigit():
            return int(s)
    return None


def normalize_config(raw: Any) -> Tuple[Dict[str, Any], List[str]]:
    errs: List[str] = []
    if not isinstance(raw, dict):
        raw = {}
        errs.append("配置文件不是 JSON 对象，已使用默认值。")
    out: Dict[str, Any] = {}
    for f in CFG_FIELDS:
        if f.key not in raw:
            if f.required:
                errs.append(f"缺少字段: {f.key}")
            out[f.key] = f.default
            continue
        v = raw.get(f.key)
        if f.kind == "str":
            s = "" if v is None else str(v)
            s = s.strip()
            if f.non_empty and not s:
                errs.append(f"字段 {f.key} 不能为空")
                out[f.key] = f.default
            else:
                out[f.key] = s
        elif f.kind == "int":
            n = _parse_int(v)
            if n is None:
                errs.append(f"字段 {f.key} 必须是整数")
                out[f.key] = f.default
            else:
                if f.min_int is not None and n < f.min_int:
                    errs.append(f"字段 {f.key} 过小: {n} < {f.min_int}")
                    out[f.key] = f.default
                else:
                    out[f.key] = n
        elif f.kind == "bool":
            b = _parse_bool(v)
            if b is None:
                errs.append(f"字段 {f.key} 必须是布尔值(0/1/true/false)")
                out[f.key] = f.default
            else:
                out[f.key] = b
        else:
            errs.append(f"未知字段类型: {f.key}")
            out[f.key] = f.default
    return out, errs


def config_is_complete(raw: Any) -> bool:
    norm, errs = normalize_config(raw)
    return len(errs) == 0 and bool(norm.get("R2_BASE_URL")) and bool(norm.get("AI_BASE_URL")) and bool(
        norm.get("AI_MODEL"))
