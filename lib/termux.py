import shlex
from typing import Any, Dict

from lib.bridge import R2BridgeClient


def termux_save_script_wrapper(bridge: R2BridgeClient, filename: str, content: str) -> Dict[str, Any]:
    target = (filename or "").strip()
    text = content if isinstance(content, str) else str(content)
    if not target:
        return {"error": "termux_save_script 需要 filename", "tool_name": "termux_save_script"}
    if not text.strip():
        return {"error": "termux_save_script 需要 content", "tool_name": "termux_save_script"}
    if target.endswith("/"):
        return {"error": "termux_save_script 的 filename 不能以 / 结尾", "tool_name": "termux_save_script"}

    is_abs = target.startswith("/")
    fname = target.rsplit("/", 1)[-1] if "/" in target else target
    if not fname or fname in {".", ".."}:
        return {"error": "termux_save_script 的 filename 不合法", "tool_name": "termux_save_script", "filename": target}

    resp = bridge.call_tool("termux_save_script", {"filename": fname, "content": text})
    if isinstance(resp, dict):
        inner = resp.get("result")
        if isinstance(inner, dict) and inner.get("isError") is True:
            content_list = inner.get("content")
            msg_text = ""
            if isinstance(content_list, list) and content_list:
                first = content_list[0]
                if isinstance(first, dict) and isinstance(first.get("text"), str):
                    msg_text = first["text"]
            return {"error": msg_text or "MCP tool error", "tool_name": "termux_save_script", "raw": resp}
    if not is_abs:
        return resp

    src = f"/data/data/com.termux/files/home/AI/{fname}"
    dst = target
    dst_dir = dst.rsplit("/", 1)[0] if "/" in dst else "/"
    cmd = f"mkdir -p {shlex.quote(dst_dir)} && cp -f {shlex.quote(src)} {shlex.quote(dst)} && echo OK"
    try:
        r = bridge.call_tool("termux_command", {"command": cmd})
        return {"saved_to_sandbox": resp, "copied_to_path": dst, "copy_result": r}
    except Exception as exc:
        return {"saved_to_sandbox": resp, "copy_error": str(exc), "copy_target": dst}
