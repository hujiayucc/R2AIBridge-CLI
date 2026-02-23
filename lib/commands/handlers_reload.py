from __future__ import annotations

import requests

from lib.analyzer import AIAnalyzer
from lib.bridge import JsonRpcError, R2BridgeClient
from lib.commands.context import CommandContext
from lib.schema import convert_tools_list_to_specs
from lib.ui_core import print_info


def handle_bridge_reload(raw: str, ctx: CommandContext) -> bool:
    if raw != "bridge_reload":
        return False
    new_url = str(ctx.current_config.get("R2_BASE_URL", "") or "").strip()
    new_timeout = int(ctx.current_config.get("MCP_TIMEOUT_S", 30) or 30)
    if not new_url:
        print_info("[bridge_reload] R2_BASE_URL 为空，请先 config set R2_BASE_URL ...")
        return True
    if ctx.analyzer is not None:
        choice = input("切换 bridge 前是否关闭当前已记录的 r2 sessions？(y/N): ").strip().lower()
        if choice == "y":
            ctx.analyzer.close_all_sessions(ctx.bridge)
    new_bridge = R2BridgeClient(base_url=new_url, timeout=new_timeout)
    try:
        h = new_bridge.health()
        listing = new_bridge.list_remote_tools()
        tools = listing.get("result", {}).get("tools") if isinstance(listing, dict) else None
        remote_specs = convert_tools_list_to_specs(tools)
        if not remote_specs:
            raise RuntimeError("tools/list 未返回有效 schema")
        ctx.schema_module.ACTIVE_TOOL_SPECS = remote_specs
        ctx.schema_loaded = True
        old_bridge = ctx.bridge
        ctx.bridge = new_bridge
        try:
            old_bridge.close()
        except (OSError, RuntimeError, AttributeError):
            pass
        print_info(f"[bridge_reload] OK health={h} tools={len(ctx.schema_module.ACTIVE_TOOL_SPECS)}")
        if ctx.analyzer is not None:
            ctx.analyzer.tool_specs = ctx.schema_module.ACTIVE_TOOL_SPECS
            print_info("[bridge_reload] 已更新 analyzer.tool_specs；建议执行 ai_reload 刷新 system prompt 中的工具清单。")
    except (requests.RequestException, JsonRpcError, ValueError, OSError, RuntimeError) as exc:
        try:
            new_bridge.close()
        except (OSError, RuntimeError, AttributeError):
            pass
        print_info(f"[bridge_reload] FAIL: {exc}")
    return True


def handle_ai_reload(raw: str, ctx: CommandContext) -> bool:
    if not raw.startswith("ai_reload"):
        return False
    parts = raw.split(" ", 1)
    mode = parts[1].strip().lower() if len(parts) >= 2 else ""
    keep = True
    if mode in {"reset", "clear"}:
        keep = False
    if mode in {"keep", ""}:
        keep = True
    if not ctx.current_config.get("AI_API_KEY"):
        print_info("[ai_reload] AI_API_KEY 为空，无法启用 AI。")
        return True
    if not ctx.schema_loaded or not isinstance(ctx.schema_module.ACTIVE_TOOL_SPECS,
                                               dict) or not ctx.schema_module.ACTIVE_TOOL_SPECS:
        print_info("[ai_reload] 未加载 tools/list schema，无法启用 AI。请先 bridge_reload 或重启。")
        return True
    old = ctx.analyzer
    try:
        ctx.analyzer = AIAnalyzer(
            api_key=str(ctx.current_config.get("AI_API_KEY", "") or ""),
            model=str(ctx.current_config.get("AI_MODEL", "") or ""),
            base_url=str(ctx.current_config.get("AI_BASE_URL", "") or ""),
            tool_specs=ctx.schema_module.ACTIVE_TOOL_SPECS,
            timeout_s=int(ctx.current_config.get("AI_TIMEOUT_S", 45) or 45),
            enable_search=bool(ctx.current_config.get("AI_ENABLE_SEARCH", False)),
            enable_thinking=bool(ctx.current_config.get("AI_ENABLE_THINKING", False)),
            thinking_budget=int(ctx.current_config.get("AI_THINKING_BUDGET", 0) or 0),
            max_tool_result_chars=int(ctx.current_config.get("MAX_TOOL_RESULT_CHARS", 5000) or 5000),
            max_context_messages=int(ctx.current_config.get("MAX_CONTEXT_MESSAGES", 40) or 40),
            max_context_chars=int(ctx.current_config.get("MAX_CONTEXT_CHARS", 140000) or 140000),
            dangerous_policy=str(ctx.current_config.get("DANGEROUS_POLICY", "confirm") or "confirm"),
            dangerous_allow_regex=str(ctx.current_config.get("DANGEROUS_ALLOW_REGEX", "") or ""),
            dangerous_extra_deny_regex=str(ctx.current_config.get("DANGEROUS_EXTRA_DENY_REGEX", "") or ""),
        )
        if keep and old is not None and isinstance(getattr(old, "messages", None), list) and len(old.messages) > 1:
            ctx.analyzer.messages.extend(old.messages[1:])
            try:
                ctx.analyzer.session_ids = set(getattr(old, "session_ids", set()) or set())
            except (TypeError, ValueError):
                pass
        print_info(f"[ai_reload] OK（{'保留上下文' if keep else '已清空上下文'}）")
    except (TypeError, ValueError, RuntimeError) as exc:
        ctx.analyzer = old
        print_info(f"[ai_reload] FAIL: {exc}")
    return True
