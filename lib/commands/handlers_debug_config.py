from __future__ import annotations

import json
import os
import time

from lib.cfg_schema import CFG_FIELDS, normalize_config
from lib.commands.context import CommandContext
from lib.config import CONFIG_SAVE_PATH
from lib.debug import (
    debug_enabled,
    debug_log_path,
    debug_max_bytes,
    read_debug_events_tail,
    read_debug_trace,
    set_debug_enabled,
    set_debug_log_path,
    set_debug_max_bytes,
)
from lib.persist import save_json_file
from lib.ui_core import print_info


def handle_debug(raw: str, ctx: CommandContext) -> bool:
    if not raw.startswith("debug"):
        return False
    parts = raw.split(" ", 3)
    if len(parts) == 1 or not parts[1].strip():
        state = "on" if debug_enabled() else "off"
        print_info(f"[debug] 当前: {state}  path={debug_log_path()}  max_bytes={debug_max_bytes()}")
        return True
    op = parts[1].strip().lower()
    if op == "tail":
        n = 30
        if len(parts) >= 3 and parts[2].strip().isdigit():
            n = int(parts[2].strip())
        events = read_debug_events_tail(max_events=n)
        if not events:
            print_info("[debug] 无事件（或日志文件不存在/为空）")
            return True
        for rec in events:
            ts = rec.get("ts")
            ev = rec.get("event")
            data = rec.get("data") if isinstance(rec.get("data"), dict) else {}
            tid = data.get("trace_id") if isinstance(data, dict) else ""
            turn = data.get("turn_id") if isinstance(data, dict) else ""
            print_info(f"[debug] {ts} {ev} trace={tid} turn={turn}")
        return True
    if op == "trace":
        if len(parts) < 3 or not parts[2].strip():
            print_info("用法: debug trace <trace_id> [n]")
            return True
        tid = parts[2].strip()
        n = 200
        if len(parts) >= 4 and parts[3].strip().isdigit():
            n = int(parts[3].strip())
        events = read_debug_trace(tid, max_events=n)
        if not events:
            print_info("[debug] 未找到该 trace_id 的事件（或日志文件不存在）")
            return True
        for rec in events:
            ts = rec.get("ts")
            ev = rec.get("event")
            data = rec.get("data")
            if isinstance(data, dict):
                short = {k: data.get(k) for k in
                         ["turn_id", "tool_name", "finish_reason", "tool_calls_count", "recoverable", "ok"]}
            else:
                short = {}
            print_info(f"[debug] {ts} {ev} {json.dumps(short, ensure_ascii=False)}")
        return True
    if op == "export":
        if len(parts) < 3 or not parts[2].strip():
            print_info("用法: debug export <trace_id|last> [out_dir]")
            return True
        tid = parts[2].strip()
        if tid == "last":
            tid = str(ctx.last_ai_trace_id or "").strip()
        if not tid:
            print_info("[debug] 未找到最近一次 trace_id（请先运行 ai 或手动指定 trace_id）")
            return True
        base_dir = parts[3].strip() if len(parts) >= 4 and parts[3].strip() else "./exports"
        stamp = int(time.time())
        out_dir = os.path.join(base_dir, f"trace_{tid}_{stamp}")
        try:
            os.makedirs(out_dir, exist_ok=True)
        except OSError as exc:
            print_info(f"[debug] 创建导出目录失败: {exc}")
            return True
        try:
            events = read_debug_trace(tid, max_events=5000)
            with open(os.path.join(out_dir, "trace.jsonl"), "w", encoding="utf-8") as f:
                for rec in events:
                    try:
                        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                    except (TypeError, ValueError):
                        continue
            with open(os.path.join(out_dir, "config.json"), "w", encoding="utf-8") as f:
                f.write(json.dumps(ctx.current_config, ensure_ascii=False, indent=2))
            status = {
                "bridge": {"base_url": getattr(ctx.bridge, "base_url", ""),
                           "timeout": getattr(ctx.bridge, "timeout", "")},
                "schema_loaded": bool(ctx.schema_loaded),
                "tools": len(ctx.schema_module.ACTIVE_TOOL_SPECS) if isinstance(ctx.schema_module.ACTIVE_TOOL_SPECS,
                                                                                dict) else 0,
                "active_session_id": ctx.active_session_id,
                "known_sessions": sorted(list(ctx.known_sessions))[:200],
                "last_ai_trace_id": ctx.last_ai_trace_id,
            }
            with open(os.path.join(out_dir, "status.json"), "w", encoding="utf-8") as f:
                f.write(json.dumps(status, ensure_ascii=False, indent=2))
            with open(os.path.join(out_dir, "README.txt"), "w", encoding="utf-8") as f:
                f.write(
                    "R2 客户端 debug 导出包\n"
                    f"- trace_id: {tid}\n"
                    f"- 目录: {out_dir}\n"
                    "\n包含文件:\n"
                    "- config.json: 启动时/运行时生效配置快照\n"
                    "- status.json: bridge/schema/session 等状态快照\n"
                    "- trace.jsonl: 该 trace_id 的 debug 事件链\n"
                )
            print_info(f"[debug] 已导出: {out_dir}")
        except (OSError, TypeError, ValueError) as exc:
            print_info(f"[debug] 导出失败: {exc}")
        return True
    if op in {"on", "1", "true", "yes", "y"}:
        set_debug_enabled(True)
        if len(parts) >= 3 and parts[2].strip():
            set_debug_log_path(parts[2].strip())
        ctx.current_config["DEBUG_ENABLED"] = True
        ctx.current_config["DEBUG_LOG_PATH"] = debug_log_path()
        try:
            save_json_file(CONFIG_SAVE_PATH, ctx.current_config)
        except (OSError, TypeError, ValueError):
            pass
        print_info(f"[debug] 已开启  path={debug_log_path()}（已保存到配置）")
        return True
    if op in {"off", "0", "false", "no", "n"}:
        set_debug_enabled(False)
        ctx.current_config["DEBUG_ENABLED"] = False
        try:
            save_json_file(CONFIG_SAVE_PATH, ctx.current_config)
        except (OSError, TypeError, ValueError):
            pass
        print_info("[debug] 已关闭（已保存到配置）")
        return True
    if op in {"path"} and len(parts) >= 3 and parts[2].strip():
        set_debug_log_path(parts[2].strip())
        ctx.current_config["DEBUG_LOG_PATH"] = debug_log_path()
        try:
            save_json_file(CONFIG_SAVE_PATH, ctx.current_config)
        except (OSError, TypeError, ValueError):
            pass
        print_info(f"[debug] path={debug_log_path()}（已保存到配置）")
        return True
    if op == "max_bytes" and len(parts) >= 3 and parts[2].strip():
        try:
            n = int(parts[2].strip())
        except (TypeError, ValueError):
            print_info("用法: debug max_bytes <n>（0=关闭轮转）")
            return True
        set_debug_max_bytes(n)
        ctx.current_config["DEBUG_MAX_BYTES"] = int(n)
        try:
            save_json_file(CONFIG_SAVE_PATH, ctx.current_config)
        except (OSError, TypeError, ValueError):
            pass
        print_info(f"[debug] max_bytes={debug_max_bytes()}（已保存到配置）")
        return True
    print_info(
        "用法: debug | debug on [path] | debug off | debug path <path> | debug tail [n] | debug trace <trace_id> [n] | debug export <trace_id> [out_dir]")
    return True


def handle_config(raw: str, ctx: CommandContext) -> bool:
    if not raw.startswith("config"):
        return False
    parts = raw.split(" ", 3)
    if len(parts) == 1 or not parts[1].strip():
        print_info("用法: config keys | config show | config set <key> <value>")
        return True
    sub = parts[1].strip().lower()
    if sub == "keys":
        keys = [f.key for f in CFG_FIELDS]
        print_info("[配置] 可配置项: " + ", ".join(keys))
        return True
    if sub == "show":
        cfg = dict(ctx.current_config)
        ak = str(cfg.get("AI_API_KEY", "") or "")
        if ak:
            cfg["AI_API_KEY"] = ak[:6] + "...(masked)"
        ordered = {k: cfg.get(k) for k in sorted(cfg.keys())}
        print(json.dumps(ordered, ensure_ascii=False, indent=2))
        return True
    if sub == "set":
        if len(parts) < 4 or (not parts[2].strip()):
            print_info("用法: config set <key> <value>")
            return True
        key = parts[2].strip()
        raw_val = parts[3] if len(parts) >= 4 else ""
        raw_val = raw_val.strip()
        if (len(raw_val) >= 2) and ((raw_val[0] == raw_val[-1]) and raw_val[0] in {"'", '"'}):
            raw_val = raw_val[1:-1]
        trial = dict(ctx.current_config)
        trial[key] = raw_val
        norm, errs = normalize_config(trial)
        bad = [e for e in errs if (key in e)]
        if bad:
            print_info("[配置] 设置失败：")
            for e in bad[:6]:
                print_info(f"- {e}")
            return True
        new_val = norm.get(key)
        ctx.current_config[key] = new_val
        try:
            save_json_file(CONFIG_SAVE_PATH, ctx.current_config)
            print_info(f"[配置] 已保存: {key}={new_val!r}")
        except (OSError, TypeError, ValueError) as exc:
            print_info(f"[配置] 保存失败: {exc}")
            return True
        if key == "DEBUG_ENABLED":
            set_debug_enabled(bool(new_val))
            print_info(f"[配置] 已热更新 debug: {'on' if debug_enabled() else 'off'}")
        elif key == "DEBUG_LOG_PATH":
            set_debug_log_path(str(new_val))
            print_info(f"[配置] 已热更新 debug path: {debug_log_path()}")
        elif key == "MCP_TIMEOUT_S":
            try:
                ctx.bridge.timeout = int(new_val)
                print_info(f"[配置] 已热更新 MCP timeout: {ctx.bridge.timeout}s")
            except (TypeError, ValueError, AttributeError):
                print_info("[配置] MCP timeout 热更新失败（建议重启）")
        elif key in {"MAX_TOOL_RESULT_CHARS", "MAX_CONTEXT_MESSAGES", "MAX_CONTEXT_CHARS"}:
            if ctx.analyzer is not None:
                if key == "MAX_TOOL_RESULT_CHARS":
                    ctx.analyzer.max_tool_result_chars = int(new_val)
                elif key == "MAX_CONTEXT_MESSAGES":
                    ctx.analyzer.max_context_messages = int(new_val)
                elif key == "MAX_CONTEXT_CHARS":
                    ctx.analyzer.max_context_chars = int(new_val)
                print_info("[配置] 已热更新 AI 裁剪预算")
            else:
                print_info("[配置] AI 未启用：将于下次启用/ai_reload 时生效")
        elif key in {"DANGEROUS_POLICY", "DANGEROUS_ALLOW_REGEX", "DANGEROUS_EXTRA_DENY_REGEX"}:
            if ctx.analyzer is not None:
                if key == "DANGEROUS_POLICY":
                    ctx.analyzer.dangerous_policy = str(new_val or "confirm").strip().lower()
                elif key == "DANGEROUS_ALLOW_REGEX":
                    ctx.analyzer.dangerous_allow_regex = str(new_val or "")
                elif key == "DANGEROUS_EXTRA_DENY_REGEX":
                    ctx.analyzer.dangerous_extra_deny_regex = str(new_val or "")
                print_info("[配置] 已热更新 危险命令策略参数")
            else:
                print_info("[配置] AI 未启用：将于下次启用/ai_reload 时生效")
        elif key in {"AI_BASE_URL", "AI_MODEL", "AI_API_KEY", "AI_TIMEOUT_S"}:
            print_info("[配置] AI 连接/模型相关参数已保存：请执行 ai_reload 使其立刻生效。")
        elif key == "R2_BASE_URL":
            print_info("[配置] R2_BASE_URL 已保存：请执行 bridge_reload 使其立刻生效。")
        return True
    print_info("用法: config keys | config show | config set <key> <value>")
    return True
