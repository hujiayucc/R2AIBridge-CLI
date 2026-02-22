from __future__ import annotations

import requests

from lib.bridge import JsonRpcError
from lib.commands.context import CommandContext
from lib.ui_core import print_info


def handle_status(raw: str, ctx: CommandContext) -> bool:
    if raw != "status":
        return False
    tool_count = len(ctx.schema_module.ACTIVE_TOOL_SPECS) if isinstance(ctx.schema_module.ACTIVE_TOOL_SPECS,
                                                                        dict) else 0
    ai_on = (ctx.analyzer is not None)
    sess = set(ctx.known_sessions)
    if ctx.analyzer is not None:
        try:
            sess.update(set(getattr(ctx.analyzer, "session_ids", set()) or set()))
        except (TypeError, ValueError, AttributeError):
            pass
    print_info("[状态] bridge:")
    print_info(f"  base_url={getattr(ctx.bridge, 'base_url', '')}  timeout={getattr(ctx.bridge, 'timeout', '')}s")
    print_info("[状态] schema:")
    print_info(f"  loaded={bool(ctx.schema_loaded)}  tools={tool_count}")
    print_info("[状态] AI:")
    print_info(
        f"  enabled={ai_on}  model={ctx.current_config.get('AI_MODEL')}  base_url={ctx.current_config.get('AI_BASE_URL')}")
    print_info("[状态] sessions:")
    print_info(f"  active={ctx.active_session_id or '(无)'}  known={len(sess)}")
    print_info("[状态] debug:")
    dbg = ctx.current_config.get("DEBUG_ENABLED")
    print_info(f"  {'on' if dbg else 'off'}  path={ctx.current_config.get('DEBUG_LOG_PATH')}")
    return True


def handle_session(raw: str, ctx: CommandContext) -> bool:
    if not raw.startswith("session"):
        return False
    parts = raw.split(" ", 2)
    sub = parts[1].strip().lower() if len(parts) >= 2 else ""
    sess = set(ctx.known_sessions)
    if ctx.analyzer is not None:
        try:
            sess.update(set(getattr(ctx.analyzer, "session_ids", set()) or set()))
        except (TypeError, ValueError, AttributeError):
            pass
    if not sub or sub == "help":
        print_info("用法: session list | session use <session_id> | session close <id|active|all>")
        return True
    if sub == "list":
        items = sorted(sess)
        print_info(f"[session] active={ctx.active_session_id or '(无)'} total={len(items)}")
        for s in items[:30]:
            tag = " *" if s == ctx.active_session_id else ""
            print_info(f"  - {s}{tag}")
        if len(items) > 30:
            print_info(f"  ...(其余 {len(items) - 30} 个已省略)")
        return True
    if sub == "use":
        if len(parts) < 3 or not parts[2].strip():
            print_info("用法: session use <session_id>")
            return True
        sid = parts[2].strip()
        if not sid.startswith("session_"):
            print_info("[session] session_id 格式应为 session_...")
            return True
        ctx.active_session_id = sid
        ctx.known_sessions.add(sid)
        print_info(f"[session] 已设置 active={sid}")
        return True
    if sub == "close":
        if len(parts) < 3 or not parts[2].strip():
            print_info("用法: session close <id|active|all>")
            return True
        target = parts[2].strip().lower()
        to_close: list[str]
        if target == "active":
            if not ctx.active_session_id:
                print_info("[session] 当前无 active session")
                return True
            to_close = [ctx.active_session_id]
        elif target == "all":
            to_close = sorted(sess)
        else:
            to_close = [parts[2].strip()]
        ok = 0
        for sid in to_close:
            try:
                ctx.bridge.call_tool("r2_close_session", {"session_id": sid})
                ok += 1
                ctx.known_sessions.discard(sid)
                if ctx.analyzer is not None:
                    try:
                        ctx.analyzer.session_ids.discard(sid)
                    except (AttributeError, TypeError):
                        pass
                if sid == ctx.active_session_id:
                    ctx.active_session_id = ""
            except KeyboardInterrupt:
                print_info("[session] 已中断关闭操作。")
                break
            except (requests.RequestException, JsonRpcError, ValueError, TypeError, OSError, RuntimeError,
                    KeyError) as exc:
                print_info(f"[session] 关闭失败 {sid}: {exc}")
        print_info(f"[session] 已关闭 {ok}/{len(to_close)}")
        return True
    print_info("用法: session list | session use <session_id> | session close <id|active|all>")
    return True
