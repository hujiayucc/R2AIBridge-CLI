from __future__ import annotations

import json

from lib.commands.context import CommandContext
from lib.ui_core import print_info


def handle_help(raw: str, _ctx: CommandContext) -> bool:
    if raw != "help":
        return False
    from lib.ui import print_help

    print_help()
    return True


def handle_health(raw: str, ctx: CommandContext) -> bool:
    if raw != "health":
        return False
    try:
        print(ctx.bridge.health())
    except Exception as exc:
        print(f"[错误] health 检查失败: {exc}")
    return True


def handle_list(raw: str, ctx: CommandContext) -> bool:
    if raw != "list":
        return False
    try:
        print(json.dumps(ctx.bridge.list_remote_tools(), ensure_ascii=False, indent=2))
    except Exception as exc:
        print(f"[错误] tools/list 调用失败: {exc}")
    return True


def handle_tools(raw: str, ctx: CommandContext) -> bool:
    if raw not in {"tools", "local_tools"}:
        return False
    ctx.schema_module.print_local_tools()
    return True


def handle_exit(raw: str, ctx: CommandContext) -> bool:
    if raw not in {"exit", "quit", "q"}:
        return False
    ctx.should_exit = True
    print_info("已退出。")
    return True
