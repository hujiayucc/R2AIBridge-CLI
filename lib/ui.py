from __future__ import annotations

import os
import re
from typing import Optional, TYPE_CHECKING

import lib.schema as schema
from lib.bridge import R2BridgeClient
from lib.cfg_schema import config_is_complete, normalize_config
from lib.cli_input import read_command
from lib.commands.context import CommandContext
from lib.commands.handlers_basic import handle_exit, handle_health, handle_help, handle_list, handle_tools
from lib.commands.handlers_debug_config import handle_config, handle_debug
from lib.commands.handlers_reload import handle_ai_reload, handle_bridge_reload
from lib.commands.handlers_selfcheck_call_ai import (
    handle_ai,
    handle_ai_reset,
    handle_call,
    handle_self_check,
    handle_workflows,
)
from lib.commands.handlers_status_session import handle_session, handle_status
from lib.commands.registry import CommandRegistry
from lib.config import *
from lib.debug import (
    debug_enabled,
    debug_log_path,
    set_debug_enabled,
    set_debug_log_path,
    set_debug_max_bytes,
)
from lib.persist import load_json_file, save_json_file, load_config
from lib.schema import convert_tools_list_to_specs
from lib.ui_core import print_info

if TYPE_CHECKING:
    from lib.analyzer import AIAnalyzer


def _kb_tokens(text: str) -> set[str]:
    t = (text or "").lower()
    tokens: set[str] = set()
    for w in re.findall(r"[a-z0-9_]{3,}", t):
        tokens.add(w)
    for w in re.findall(r"[\u4e00-\u9fff]{2,}", text or ""):
        tokens.add(w)
    return tokens


def _contains_dsml_markup(text: str) -> bool:
    t = text or ""
    return (
            ("<｜DSML｜" in t)
            or ("<|DSML|" in t)
            or ("</｜DSML｜" in t)
            or ("</|DSML|" in t)
            or (re.search(r"<[|｜]DSML[|｜](invoke|parameter)\b", t, flags=re.IGNORECASE) is not None)
    )


def _kb_score_item(query_tokens: set[str], item: dict) -> int:
    if not query_tokens:
        return 0
    q = str(item.get("question", "") or "")
    findings = item.get("key_findings") or []
    if not isinstance(findings, list):
        findings = []
    blob = q + "\n" + "\n".join(str(x) for x in findings[:20])
    blob_low = blob.lower()
    score = 0
    for tok in query_tokens:
        if not tok:
            continue
        if tok.lower() in blob_low:
            score += 3
    return score


def _kb_build_context(
        question: str,
        kb_items: list[dict],
        max_items: int = 3,
        max_chars: int = 1400,
) -> tuple[str, list[dict]]:
    if not kb_items:
        return "", []
    q_tokens = _kb_tokens(question)
    scored: list[tuple[int, dict]] = []
    for it in kb_items:
        if not isinstance(it, dict):
            continue
        s = _kb_score_item(q_tokens, it)
        if s > 0:
            scored.append((s, it))
    if not scored:
        return "", []
    scored.sort(key=lambda x: x[0], reverse=True)
    picked = [it for _, it in scored[:max_items]]
    lines: list[str] = ["【知识库参考（仅供提示，结论需用工具再次取证）】"]
    used = len(lines[0]) + 1
    for it in picked:
        kid = str(it.get("id", "") or "").strip()
        q = str(it.get("question", "") or "").strip()
        if kid or q:
            head = f"- {kid} {q}".strip()
            if used + len(head) + 1 > max_chars:
                break
            lines.append(head)
            used += len(head) + 1
        findings = it.get("key_findings") or []
        if isinstance(findings, list) and findings:
            for f in findings[:6]:
                s = str(f).strip()
                if not s:
                    continue
                row = f"  * {s}"
                if used + len(row) + 1 > max_chars:
                    break
                lines.append(row)
                used += len(row) + 1
        md = str(it.get("final_markdown", "") or "").strip()
        if md:
            excerpt = re.sub(r"\s+", " ", md)[:220]
            row = f"  * 摘要: {excerpt}..."
            if used + len(row) + 1 <= max_chars:
                lines.append(row)
                used += len(row) + 1
    return "\n".join(lines).strip(), picked


def _extract_markdown_section(md: str, heading: str) -> str:
    text = (md or "").replace("\r\n", "\n").replace("\r", "\n")
    if not text.strip():
        return ""
    pat = re.compile(
        rf"(?m)^\s*##\s+{re.escape(heading)}\s*$\n(.*?)(?=^\s*##\s+|\Z)",
        flags=re.DOTALL,
    )
    m = pat.search(text)
    return (m.group(1) if m else "").strip()


def _extract_key_findings(md: str, limit: int = 12) -> list[str]:
    sec = _extract_markdown_section(md, "关键发现")
    if not sec:
        return []
    out: list[str] = []
    for raw in sec.split("\n"):
        line = raw.strip()
        if not line:
            continue
        line = re.sub(r"^[-*]\s+", "", line)
        line = re.sub(r"^\d+\.\s+", "", line)
        line = line.strip()
        if not line:
            continue
        out.append(line)
        if len(out) >= limit:
            break
    return out


def _append_kb_item(kb_path: str, item: dict) -> None:
    kb = load_json_file(kb_path, {"items": []})
    if not isinstance(kb, dict):
        kb = {"items": []}
    items = kb.get("items")
    if not isinstance(items, list):
        items = []
        kb["items"] = items
    items.append(item)
    save_json_file(kb_path, kb)


def print_help() -> None:
    print("\n可用命令:")
    print("  help                           查看帮助")
    print("  health                         检查服务健康状态")
    print("  self_check                     一次性自检（bridge/工具/AI key/python）")
    print("  tools                          查看当前工具 schema（以 tools/list 为准）")
    print("  list                           请求服务端 tools/list")
    print('  call <工具名> [JSON参数]         调用工具，例如: call r2_test 或 call r2_open_file {"file_path":"/..."}')
    print("  ai [--strict|--loose|--plain] <问题>  AI 问答/分析（默认 loose；strict=强制取证+最终报告门禁）")
    print("  ai --tools                      直接列出当前可用工具（不调用 AI）")
    print("  ai_reset                        清空 AI 对话上下文")
    print("  ai_reload [keep|reset]          重新初始化 AI（让 AI 配置立刻生效）")
    print("  bridge_reload                   重新连接 bridge 并刷新 tools/list（让 R2 配置立刻生效）")
    print("  debug [on|off] [path]          切换/查看 debug 日志（JSONL），例如: debug on 或 debug on ./debug.jsonl")
    print("  debug tail [n]                 查看最近 n 条 debug 事件（默认 30）")
    print("  debug trace <trace_id> [n]     查看某次 trace 的事件链（默认 200）")
    print("  config keys                    列出所有可配置项")
    print("  config show                    显示当前配置（部分字段脱敏）")
    print("  config set <key> <value>       修改配置并保存（可热更新的项会立即生效）")
    print("  status                         显示当前状态（bridge/AI/schema/session/debug）")
    print("  session list                   列出已知 r2 sessions")
    print("  session use <session_id>       设置当前 active session（手动 call 可自动补齐）")
    print("  session close <id|active|all>  关闭 session")
    print("  exit                            退出程序\n")


def main() -> None:
    from lib.analyzer import AIAnalyzer
    kb = load_json_file(KB_SAVE_PATH, {"items": []})
    kb_count = len(kb.get("items", [])) if isinstance(kb, dict) and isinstance(kb.get("items"), list) else 0
    print_info(f"[知识库] 已加载: {KB_SAVE_PATH} (共 {kb_count} 条)")
    kb_items: list[dict] = []
    if isinstance(kb, dict) and isinstance(kb.get("items"), list):
        kb_items = [x for x in kb.get("items", []) if isinstance(x, dict)]

    raw_cfg = load_json_file(CONFIG_SAVE_PATH, None)
    saved = load_config(CONFIG_SAVE_PATH)
    has_cfg_file = isinstance(raw_cfg, dict)
    print(f"[配置] 已加载历史配置: {CONFIG_SAVE_PATH}")

    norm_cfg, cfg_errs = normalize_config(raw_cfg)
    if cfg_errs:
        print_info("[配置] 检测到配置项不完整/不合法，已自动回退默认值并将于启动时重写。")
        for e in cfg_errs[:6]:
            print_info(f"[配置] - {e}")

    dbg_on = bool(saved.get("DEBUG_ENABLED") is True)
    set_debug_enabled(dbg_on)
    cfg_dbg_path = str(saved.get("DEBUG_LOG_PATH", "") or "").strip()
    if cfg_dbg_path:
        set_debug_log_path(cfg_dbg_path)
    try:
        set_debug_max_bytes(int(saved.get("DEBUG_MAX_BYTES", 0) or 0))
    except (TypeError, ValueError):
        set_debug_max_bytes(0)

    skip_setup = False
    if has_cfg_file and config_is_complete(raw_cfg):
        choice = input("检测到配置文件已完整，是否跳过配置环节直接启动？(Y/n): ").strip().lower()
        skip_setup = (choice != "n")

    if skip_setup:
        base_url = str(saved["R2_BASE_URL"]).strip()
        mcp_timeout_s = int(saved.get("MCP_TIMEOUT_S", 30))
        ai_base_url = str(saved["AI_BASE_URL"]).strip()
        ai_model = str(saved["AI_MODEL"]).strip()
        api_key = str(saved.get("AI_API_KEY", "") or "")
        ai_enable_search = bool(saved.get("AI_ENABLE_SEARCH", False))
        ai_enable_thinking = bool(saved.get("AI_ENABLE_THINKING", False))
        ai_timeout_s = int(saved.get("AI_TIMEOUT_S", 45))
        max_tool_result_chars = int(saved.get("MAX_TOOL_RESULT_CHARS", 5000))
        max_context_messages = int(saved.get("MAX_CONTEXT_MESSAGES", 40))
        max_context_chars = int(saved.get("MAX_CONTEXT_CHARS", 140000))
        dangerous_policy = str(saved.get("DANGEROUS_POLICY", "confirm") or "confirm").strip().lower()
        dangerous_allow_regex = str(saved.get("DANGEROUS_ALLOW_REGEX", "") or "")
        dangerous_extra_deny_regex = str(saved.get("DANGEROUS_EXTRA_DENY_REGEX", "") or "")
    else:
        base_url = input(f"R2 服务地址（默认 {saved['R2_BASE_URL']}）: ").strip() or str(saved["R2_BASE_URL"])
        adv = input("是否配置高级参数（timeout/裁剪预算）？(y/N): ").strip().lower() == "y"
        if adv:
            mcp_timeout_s = int(
                input(f"MCP/HTTP timeout 秒（默认 {saved['MCP_TIMEOUT_S']}）: ").strip() or saved["MCP_TIMEOUT_S"])
        else:
            mcp_timeout_s = int(saved.get("MCP_TIMEOUT_S", 30))

        env_api_key = os.getenv("R2_AI_API_KEY", "").strip()
        env_ai_base_url = os.getenv("R2_AI_BASE_URL", "").strip()
        env_ai_model = os.getenv("R2_AI_MODEL", "").strip()

        ai_base_url = env_ai_base_url or input(f"AI Base URL（默认 {saved['AI_BASE_URL']}）: ").strip() or str(
            saved["AI_BASE_URL"])
        ai_model = env_ai_model or input(f"AI Model（默认 {saved['AI_MODEL']}）: ").strip() or str(saved["AI_MODEL"])
        ai_base_lower = str(ai_base_url or "").strip().lower()
        is_dashscope = ("dashscope.aliyuncs.com" in ai_base_lower)
        is_deepseek = ("api.deepseek.com" in ai_base_lower)

        if env_api_key:
            api_key = env_api_key
        else:
            hint = f"{str(saved.get('AI_API_KEY', '') or '')[:10]}..." if saved.get("AI_API_KEY") else "空"
            api_key = input(f"AI API Key（回车复用已保存值 {hint}，输入新值覆盖）: ").strip() or str(
                saved.get("AI_API_KEY", "") or "")

        if adv:
            ai_timeout_s = int(
                input(f"AI 请求 timeout 秒（默认 {saved['AI_TIMEOUT_S']}）: ").strip() or saved["AI_TIMEOUT_S"])
            max_tool_result_chars = int(
                input(f"单次工具结果最大保留字符数（默认 {saved['MAX_TOOL_RESULT_CHARS']}）: ").strip() or saved[
                    "MAX_TOOL_RESULT_CHARS"])
            max_context_messages = int(
                input(f"对话上下文最大消息数（默认 {saved['MAX_CONTEXT_MESSAGES']}）: ").strip() or saved[
                    "MAX_CONTEXT_MESSAGES"])
            max_context_chars = int(
                input(f"对话上下文最大字符预算（默认 {saved['MAX_CONTEXT_CHARS']}）: ").strip() or saved[
                    "MAX_CONTEXT_CHARS"])
            if is_dashscope:
                dflt = "y" if bool(saved.get("AI_ENABLE_SEARCH", False)) else "n"
                choice = input(
                    f"启用 AI 联网搜索（DashScope enable_search；默认 {dflt}）(y/N): ").strip().lower()
                ai_enable_search = choice in {"y", "yes", "1", "true", "on"}
            else:
                ai_enable_search = False

            model_lower = str(ai_model or "").strip().lower()
            if is_deepseek and (model_lower != "deepseek-reasoner"):
                dflt = "y" if bool(saved.get("AI_ENABLE_THINKING", False)) else "n"
                choice = input(
                    f"启用 DeepSeek 思考模式（extra_body.thinking；默认 {dflt}）(y/N): ").strip().lower()
                ai_enable_thinking = choice in {"y", "yes", "1", "true", "on"}
            else:
                ai_enable_thinking = False
        else:
            ai_timeout_s = int(saved.get("AI_TIMEOUT_S", 45))
            max_tool_result_chars = int(saved.get("MAX_TOOL_RESULT_CHARS", 5000))
            max_context_messages = int(saved.get("MAX_CONTEXT_MESSAGES", 40))
            max_context_chars = int(saved.get("MAX_CONTEXT_CHARS", 140000))
            ai_enable_search = bool(saved.get("AI_ENABLE_SEARCH", False))
            ai_enable_thinking = bool(saved.get("AI_ENABLE_THINKING", False))
        dangerous_policy = str(saved.get("DANGEROUS_POLICY", "confirm") or "confirm").strip().lower()
        dangerous_allow_regex = str(saved.get("DANGEROUS_ALLOW_REGEX", "") or "")
        dangerous_extra_deny_regex = str(saved.get("DANGEROUS_EXTRA_DENY_REGEX", "") or "")

    ai_base_lower2 = str(ai_base_url or "").strip().lower()
    is_dashscope2 = ("dashscope.aliyuncs.com" in ai_base_lower2)
    is_deepseek2 = ("api.deepseek.com" in ai_base_lower2)
    model_lower2 = str(ai_model or "").strip().lower()
    if ai_enable_search and (not is_dashscope2):
        ai_enable_search = False
    if ai_enable_thinking and (not is_deepseek2):
        ai_enable_thinking = False
    if ai_enable_thinking and is_deepseek2 and (model_lower2 == "deepseek-reasoner"):
        ai_enable_thinking = False

    bridge = R2BridgeClient(base_url=base_url, timeout=mcp_timeout_s)
    import atexit
    atexit.register(bridge.close)

    schema_loaded = False
    try:
        listing = bridge.list_remote_tools()
        tools = listing.get("result", {}).get("tools") if isinstance(listing, dict) else None
        remote_specs = convert_tools_list_to_specs(tools)
        if remote_specs:
            schema.ACTIVE_TOOL_SPECS = remote_specs
            print_info(f"[工具] 已从服务端 tools/list 加载 schema: {len(schema.ACTIVE_TOOL_SPECS)} 个")
            schema_loaded = True
    except Exception as exc:
        print_info(f"[警告] tools/list 获取失败，继续使用本地工具 schema: {exc}")
        schema_loaded = False

    current_config = {
        "R2_BASE_URL": base_url,
        "AI_BASE_URL": ai_base_url,
        "AI_MODEL": ai_model,
        "AI_API_KEY": api_key,
        "AI_ENABLE_SEARCH": bool(ai_enable_search),
        "AI_ENABLE_THINKING": bool(ai_enable_thinking),
        "DEBUG_ENABLED": bool(debug_enabled()),
        "DEBUG_LOG_PATH": debug_log_path(),
        "MCP_TIMEOUT_S": int(mcp_timeout_s),
        "AI_TIMEOUT_S": int(ai_timeout_s),
        "MAX_TOOL_RESULT_CHARS": int(max_tool_result_chars),
        "MAX_CONTEXT_MESSAGES": int(max_context_messages),
        "MAX_CONTEXT_CHARS": int(max_context_chars),
        "DANGEROUS_POLICY": str(saved.get("DANGEROUS_POLICY", "confirm") or "confirm"),
        "DANGEROUS_ALLOW_REGEX": str(saved.get("DANGEROUS_ALLOW_REGEX", "") or ""),
        "DANGEROUS_EXTRA_DENY_REGEX": str(saved.get("DANGEROUS_EXTRA_DENY_REGEX", "") or ""),
        "DEBUG_MAX_BYTES": int(saved.get("DEBUG_MAX_BYTES", 0) or 0),
    }
    save_json_file(CONFIG_SAVE_PATH, current_config)
    print("[配置] 已在启动时保存当前配置，下次可直接复用。")

    analyzer: Optional[AIAnalyzer] = None
    if api_key:
        try:
            if not schema_loaded:
                raise RuntimeError("未能从 tools/list 加载工具 schema，已禁用 ai/search（仍可使用 call）。")
            analyzer = AIAnalyzer(
                api_key=api_key,
                model=ai_model,
                base_url=ai_base_url,
                tool_specs=schema.ACTIVE_TOOL_SPECS,
                timeout_s=int(ai_timeout_s),
                enable_search=bool(ai_enable_search),
                enable_thinking=bool(ai_enable_thinking),
                max_tool_result_chars=int(max_tool_result_chars),
                max_context_messages=int(max_context_messages),
                max_context_chars=int(max_context_chars),
                dangerous_policy=str(dangerous_policy),
                dangerous_allow_regex=str(dangerous_allow_regex),
                dangerous_extra_deny_regex=str(dangerous_extra_deny_regex),
            )
            session = load_json_file(SESSION_SAVE_PATH, None)
            if isinstance(session, dict):
                choice = input("检测到上次 AI 会话，是否加载？(y/N): ").strip().lower()
                if choice == "y":
                    if analyzer.load_session(session):
                        print_info("[会话] 已加载上次会话，可继续多轮对话。")
                    else:
                        print_info("[会话] 会话文件无效，已忽略。")
        except Exception as exc:
            print(f"[警告] AI 初始化失败，将仅保留手动 call 模式: {exc}")

    ctx = CommandContext(
        bridge=bridge,
        schema_module=schema,
        schema_loaded=bool(schema_loaded),
        current_config=current_config,
        analyzer=analyzer,
        kb_items=kb_items,
    )

    print("\nR2AIBridge 客户端已启动。输入 help 查看命令。")
    try:
        reg = CommandRegistry()
        reg.add(handle_exit)
        reg.add(handle_help)
        reg.add(handle_health)
        reg.add(handle_tools)
        reg.add(handle_list)
        reg.add(handle_status)
        reg.add(handle_session)
        reg.add(handle_debug)
        reg.add(handle_config)
        reg.add(handle_bridge_reload)
        reg.add(handle_ai_reload)
        reg.add(handle_self_check)
        reg.add(handle_ai_reset)
        reg.add(handle_call)
        reg.add(handle_workflows)
        reg.add(handle_ai)

        while True:
            try:
                raw = read_command("r2> ", ctx)
            except KeyboardInterrupt:
                print_info("\n[提示] 已中断当前输入。")
                continue
            if not raw:
                continue

            if reg.dispatch(raw, ctx):
                if ctx.should_exit:
                    try:
                        save_json_file(CONFIG_SAVE_PATH, current_config)
                        print("[配置] 当前配置已保存。")
                    except (OSError, TypeError, ValueError) as exc:
                        print(f"[警告] 配置保存失败: {exc}")
                    if ctx.analyzer is not None:
                        ctx.analyzer.close_all_sessions(ctx.bridge)
                        if len(ctx.analyzer.messages) > 1:
                            save_choice = input("是否保存当前 AI 会话供下次启动加载？(y/N): ").strip().lower()
                            if save_choice == "y":
                                save_json_file(SESSION_SAVE_PATH, ctx.analyzer.export_session())
                                print_info(f"[会话] 当前会话已保存: {SESSION_SAVE_PATH}")
                    break
                continue
            print("未知命令，输入 help 查看可用命令。")
    finally:
        bridge.close()
