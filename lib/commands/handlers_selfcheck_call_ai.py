from __future__ import annotations

import json
import re

import requests

from lib.bridge import JsonRpcError
from lib.commands.context import CommandContext
from lib.commands.helpers import extract_session_ids
from lib.config import KB_SAVE_PATH
from lib.kb import (
    append_kb_item,
    build_kb_item,
    contains_dsml_markup,
    kb_build_context,
)
from lib.schema import validate_args
from lib.termux import termux_save_script_wrapper
from lib.ui_core import UserInterruptError, print_info, print_markdown


def _render_tools_markdown(tool_specs: object) -> str:
    if not isinstance(tool_specs, dict) or not tool_specs:
        return "## 可用工具\n\n（当前未加载 tools/list schema，工具列表为空）\n"

    def _group(n: str) -> str:
        if n.startswith("r2_"):
            return "r2_*（radare2 会话/分析）"
        if n.startswith("termux_"):
            return "termux_*（Android/Termux 命令/脚本）"
        if n.startswith("os_"):
            return "os_*（文件/目录/文本读取）"
        if n.startswith("sqlite_") or n == "sqlite_query":
            return "sqlite_*（数据库查询）"
        if n.startswith("read_logcat") or n == "read_logcat":
            return "logcat（运行时日志）"
        return "其他"

    groups: dict[str, list[str]] = {}
    for k in tool_specs.keys():
        tool_name_str = str(k or "").strip()
        if not tool_name_str:
            continue
        groups.setdefault(_group(tool_name_str), []).append(tool_name_str)

    lines: list[str] = ["## 可用工具（来自 tools/list schema）", ""]
    for gname in sorted(groups.keys()):
        lines.append(f"### {gname}")
        for tool_name in sorted(groups[gname]):
            spec = tool_specs.get(tool_name) if isinstance(tool_specs, dict) else None
            required: list[str] = []
            optional: list[str] = []
            if isinstance(spec, dict):
                req = spec.get("required")
                props = spec.get("properties")
                if isinstance(req, list):
                    required = [str(x) for x in req if isinstance(x, str) and x.strip()]
                if isinstance(props, dict):
                    optional = [str(x) for x in props.keys() if isinstance(x, str) and x not in required]
            req_s = ", ".join(required) if required else "-"
            opt_s = ", ".join(sorted(optional)[:24]) if optional else "-"
            lines.append(f"- `{tool_name}`")
            lines.append(f"  - required: {req_s}")
            lines.append(f"  - optional: {opt_s}")
        lines.append("")
    lines.append("说明：该列表由服务端 schema 决定；更详细的行为请以服务端实现为准。")
    return "\n".join(lines).strip() + "\n"


def handle_self_check(raw: str, ctx: CommandContext) -> bool:
    if raw != "self_check":
        return False
    try:
        import shutil
        cmd_python: str = "python"
        cmd_py: str = "py"
        py1 = shutil.which(cmd_python) or ""
        py2 = shutil.which(cmd_py) or ""
        if py1 or py2:
            print_info(f"[自检] Python: OK ({py1 or py2})")
        else:
            print_info("[自检] Python: 未找到（如需跑 tests 请安装 Python 并加入 PATH）")
    except (ImportError, OSError, AttributeError, TypeError, ValueError) as exc:
        print_info(f"[自检] Python 检查失败: {exc}")

    try:
        print_info("[自检] 当前生效配置:")
        print_info(f"  R2_BASE_URL={ctx.current_config.get('R2_BASE_URL')}")
        print_info(f"  MCP_TIMEOUT_S={ctx.current_config.get('MCP_TIMEOUT_S')}")
        print_info(f"  AI_BASE_URL={ctx.current_config.get('AI_BASE_URL')}")
        print_info(f"  AI_MODEL={ctx.current_config.get('AI_MODEL')}")
        print_info(f"  AI_TIMEOUT_S={ctx.current_config.get('AI_TIMEOUT_S')}")
        print_info(f"  MAX_TOOL_RESULT_CHARS={ctx.current_config.get('MAX_TOOL_RESULT_CHARS')}")
        print_info(f"  MAX_CONTEXT_MESSAGES={ctx.current_config.get('MAX_CONTEXT_MESSAGES')}")
        print_info(f"  MAX_CONTEXT_CHARS={ctx.current_config.get('MAX_CONTEXT_CHARS')}")
        print_info(f"  DANGEROUS_POLICY={ctx.current_config.get('DANGEROUS_POLICY')}")
        print_info(f"  DANGEROUS_ALLOW_REGEX={ctx.current_config.get('DANGEROUS_ALLOW_REGEX')}")
        print_info(f"  DANGEROUS_EXTRA_DENY_REGEX={ctx.current_config.get('DANGEROUS_EXTRA_DENY_REGEX')}")
        print_info(f"  DEBUG_MAX_BYTES={ctx.current_config.get('DEBUG_MAX_BYTES')}")
        dbg_state = "on" if ctx.current_config.get("DEBUG_ENABLED") else "off"
        print_info(f"  DEBUG={dbg_state} path={ctx.current_config.get('DEBUG_LOG_PATH')}")
    except (AttributeError, TypeError, KeyError, ValueError) as exc:
        print_info(f"[自检] 生效配置展示失败: {exc}")

    try:
        h = ctx.bridge.health()
        print_info(f"[自检] bridge health: OK ({h})")
    except (requests.RequestException, JsonRpcError, ValueError, OSError, RuntimeError) as exc:
        print_info(f"[自检] bridge health: FAIL ({exc})")
    try:
        listing = ctx.bridge.list_remote_tools()
        tools = listing.get("result", {}).get("tools") if isinstance(listing, dict) else None
        n = len(tools) if isinstance(tools, list) else 0
        print_info(f"[自检] tools/list: OK (tools={n})")
    except (requests.RequestException, JsonRpcError, ValueError, OSError, RuntimeError) as exc:
        print_info(f"[自检] tools/list: FAIL ({exc})")

    if ctx.current_config.get("AI_API_KEY"):
        print_info("[自检] AI_API_KEY: OK（已配置）")
    else:
        print_info("[自检] AI_API_KEY: 空（ai 命令将不可用）")
    if ctx.analyzer is not None:
        print_info("[自检] AI: 已启用（ai 命令可用）")
    else:
        print_info("[自检] AI: 未启用（仅 call 模式）")
    sess = set(ctx.known_sessions)
    if ctx.analyzer is not None:
        try:
            sess.update(set(getattr(ctx.analyzer, "session_ids", set()) or set()))
        except (TypeError, ValueError, AttributeError):
            pass
    print_info(f"[自检] sessions: active={ctx.active_session_id or '(无)'} known={len(sess)}")
    return True


def handle_ai_reset(raw: str, ctx: CommandContext) -> bool:
    if raw != "ai_reset":
        return False
    if ctx.analyzer is None:
        print("[提示] AI 未启用，请先设置 AI_API_KEY 并 ai_reload。")
    else:
        ctx.analyzer.reset()
        print("AI 对话上下文已清空。")
    return True


def handle_call(raw: str, ctx: CommandContext) -> bool:
    if not raw.startswith("call "):
        return False
    try:
        rest = raw[len("call "):].strip()
        if not rest:
            print("格式错误，请使用: call <工具名> [JSON参数]")
            return True

        tool_name, sep, tail = rest.partition(" ")
        tool_name = tool_name.strip()
        tail = tail.strip() if sep else ""
        if not tool_name:
            print("格式错误，请使用: call <工具名> [JSON参数]")
            return True

        force = False
        json_part = tail
        if tail.startswith("--force"):
            force = True
            json_part = tail[len("--force"):].strip()

        if json_part and json_part.strip():
            args = json.loads(json_part)
            if not isinstance(args, dict):
                raise ValueError("参数必须是 JSON 对象")
        else:
            args = {}
        if tool_name == "termux_command" and (not force):
            policy = str(ctx.current_config.get("DANGEROUS_POLICY", "confirm") or "confirm").strip().lower()
            if policy != "off":
                cmd = str(args.get("command") or args.get("cmd") or args.get("shell") or "")
                allow_re = str(ctx.current_config.get("DANGEROUS_ALLOW_REGEX", "") or "").strip()
                extra_deny = str(ctx.current_config.get("DANGEROUS_EXTRA_DENY_REGEX", "") or "").strip()
                if ctx.analyzer is not None:
                    ctx.analyzer.dangerous_policy = policy
                    ctx.analyzer.dangerous_allow_regex = allow_re
                    ctx.analyzer.dangerous_extra_deny_regex = extra_deny
                    is_danger, reason = ctx.analyzer.dangerous_action_for_termux_command(cmd)
                else:
                    from lib.analyzer import AIAnalyzer
                    is_danger, reason = AIAnalyzer.is_dangerous_termux_command(cmd)
                if is_danger:
                    print_info(f"[提示] 检测到危险命令（{reason}）: {cmd}")
                    if policy == "deny":
                        print_info("[提示] 当前策略为 deny，已阻止。可用: call termux_command --force {...} 强制执行。")
                        return True
                    yn = input("是否继续执行该命令？(y/N): ").strip().lower()
                    if yn != "y":
                        print_info("[提示] 已取消执行。可用: call termux_command --force {...} 强制执行。")
                        return True

        spec = ctx.schema_module.ACTIVE_TOOL_SPECS.get(tool_name) if isinstance(ctx.schema_module.ACTIVE_TOOL_SPECS,
                                                                                dict) else None
        if (
                isinstance(spec, dict)
                and isinstance(spec.get("required"), list)
                and ("session_id" in spec.get("required"))
                and ("session_id" not in args)
                and ctx.active_session_id
        ):
            args["session_id"] = ctx.active_session_id

        if tool_name == "termux_save_script":
            err = validate_args(tool_name, args, ctx.schema_module.ACTIVE_TOOL_SPECS)
            if err:
                raise ValueError(err)
            resp = termux_save_script_wrapper(
                ctx.bridge,
                filename=str(args["filename"]),
                content=str(args["content"]),
            )
            print(json.dumps(resp, ensure_ascii=False, indent=2))
        else:
            err = validate_args(tool_name, args, ctx.schema_module.ACTIVE_TOOL_SPECS)
            if err:
                raise ValueError(err)
            resp = ctx.bridge.call_tool(tool_name, args)
            print(json.dumps(resp, ensure_ascii=False, indent=2))
            sids = extract_session_ids(resp)
            if sids:
                ctx.known_sessions.update(sids)
                ctx.active_session_id = sorted(sids)[-1]
    except KeyboardInterrupt:
        print_info("[提示] 已中断当前工具调用。")
    except (json.JSONDecodeError, ValueError, TypeError, requests.RequestException, JsonRpcError, OSError,
            RuntimeError) as exc:
        print(f"[错误] 工具调用失败: {exc}")
    return True


def _run_ai_question(ctx: CommandContext, question: str, *, mode: str = "loose") -> str:
    if ctx.analyzer is None:
        return "[提示] AI 未启用，请先设置 AI_API_KEY 并 ai_reload。"
    kb_ctx, kb_picked = kb_build_context(question, ctx.kb_items)
    if kb_ctx and kb_picked:
        ids = [str(x.get("id", "") or "").strip() for x in kb_picked]
        ids = [x for x in ids if x]
        msg = f"[知识库] 已注入 {len(kb_picked)} 条参考"
        if ids:
            msg += ": " + ", ".join(ids[:6])
        print_info(msg)
    prompt = (kb_ctx + "\n\n" if kb_ctx else "") + question
    try:
        result = ctx.analyzer.chat(prompt, ctx.bridge, mode=mode)
    except UserInterruptError:
        return "已中断当前 AI 思考。"
    try:
        ctx.known_sessions.update(set(getattr(ctx.analyzer, "session_ids", set()) or set()))
    except (TypeError, ValueError, AttributeError):
        pass
    if ctx.known_sessions:
        ctx.active_session_id = sorted(list(ctx.known_sessions))[-1]
    try:
        ctx.last_ai_trace_id = str(getattr(ctx.analyzer, "last_trace_id", "") or "")
    except (TypeError, ValueError, AttributeError):
        ctx.last_ai_trace_id = ""
    return result


def handle_ai(raw: str, ctx: CommandContext) -> bool:
    if raw == "ai" or raw.startswith("ai "):
        pass
    else:
        return False
    rest = raw[2:].strip()
    if not rest:
        print("用法: ai [--strict|--loose|--plain|--tools] <问题>")
        return True

    mode = "loose"
    question = rest
    if rest.startswith("--tools"):
        md = _render_tools_markdown(ctx.schema_module.ACTIVE_TOOL_SPECS)
        print_markdown(md)
        return True
    if rest.startswith("--strict "):
        mode = "strict"
        question = rest[len("--strict "):].strip()
    elif rest.startswith("--loose "):
        mode = "loose"
        question = rest[len("--loose "):].strip()
    elif rest.startswith("--plain "):
        mode = "loose"
        question = rest[len("--plain "):].strip()

    if not question:
        print("请输入问题，例如: ai --strict 先打开 /sdcard/a.so 并列出函数")
        return True

    if (
            (mode != "strict")
            and re.search(r"(可用工具|工具列表|功能列表|tools/list)", question)
            and re.search(r"(不做分析|不要分析|只|仅|列出|清单)", question)
    ):
        md = _render_tools_markdown(ctx.schema_module.ACTIVE_TOOL_SPECS)
        print_markdown(md)
        return True

    if ":\\" in question:
        print("[提示] 你输入的是 Windows 路径。R2 服务通常运行在 Android/Termux 端，请改用 /storage/... 绝对路径。")
    try:
        result = _run_ai_question(ctx, question, mode=mode)
        while True:
            if result == "已中断当前 AI 思考。":
                print_info("[提示] 已中断当前 AI 思考。")
                break
            save_choice = input("是否将本次最终结果写入知识库？(y/N): ").strip().lower()
            if save_choice == "y":
                dsml_found = contains_dsml_markup(result)
                force = "n"
                if dsml_found:
                    force = input(
                        "检测到内容包含 DSML/协议片段，默认不写入知识库。是否仍强制写入？(y/N): "
                    ).strip().lower()
                    if force != "y":
                        print_info("[知识库] 已跳过写入（原因：检测到 DSML 内容）。")
                    else:
                        print_info("[知识库] 警告：仍将写入包含 DSML 的内容（不推荐）。")
                item = build_kb_item(question, result)
                if (not dsml_found) or (force == "y"):
                    append_kb_item(KB_SAVE_PATH, item)
                    ctx.kb_items.append(item)
                    print_info(f"[知识库] 已写入: {KB_SAVE_PATH}")

            cont_choice = input("是否继续上一轮 AI 分析？(y/N): ").strip().lower()
            if cont_choice == "y":
                result = _run_ai_question(
                    ctx,
                    "继续上一轮未完成的分析：从你最后一步开始推进，必须使用 tool_calls 执行需要的操作；"
                    "直到输出最终 Markdown（## 关键发现/## 证据来源/## 下一步建议）才停止。",
                    mode="strict",
                )
            else:
                break
    except KeyboardInterrupt:
        print_info("[提示] 已中断当前 AI 分析。")
    except (requests.RequestException, JsonRpcError, ValueError, TypeError, OSError, RuntimeError) as exc:
        print(f"[错误] AI 分析失败: {exc}")
    return True


def handle_workflows(raw: str, ctx: CommandContext) -> bool:
    def _call_tool(name: str, args: dict) -> dict:
        err = validate_args(name, args, ctx.schema_module.ACTIVE_TOOL_SPECS)
        if err:
            raise ValueError(f"{name}: {err}")
        return ctx.bridge.call_tool(name, args)

    def _parse_flag_and_path(cmd: str, prefix: str) -> tuple[str, str]:
        rest = cmd[len(prefix):].strip()
        parsed_mode = "deep"
        if rest.startswith("--fast "):
            parsed_mode = "fast"
            rest = rest[len("--fast "):].strip()
        elif rest.startswith("--deep "):
            parsed_mode = "deep"
            rest = rest[len("--deep "):].strip()
        return parsed_mode, rest

    if raw.startswith("apk_analyze "):
        analysis_mode, apk_path = _parse_flag_and_path(raw, "apk_analyze")
        if not apk_path:
            print_info("用法: apk_analyze [--fast|--deep] <apk_path>")
            return True
        try:
            print_info("[apk_analyze] 1) 检查文件存在性/大小")
            _call_tool("termux_command", {"command": f"ls -la \"{apk_path}\""})

            print_info("[apk_analyze] 2) 列出 APK 头部文件清单")
            _call_tool("termux_command", {"command": f"unzip -l \"{apk_path}\" | sed -n '1,80p'"})

            tmp_dir = "$HOME/AI/tmp_apk"
            print_info("[apk_analyze] 3) 解压 classes*.dex 到临时目录")
            out = _call_tool(
                "termux_command",
                {"command": f"mkdir -p {tmp_dir} && unzip -o -j \"{apk_path}\" \"classes*.dex\" -d {tmp_dir}"},
            )
            _call_tool("termux_command", {"command": f"ls -la {tmp_dir} | sed -n '1,120p'"})

            stdout = ""
            if isinstance(out, dict):
                stdout = str(out.get("stdout") or out.get("output") or "")
            dex_name = "classes.dex"
            try:
                dex_match = re.search(r"(classes\\d*\\.dex)", stdout)
            except re.error:
                dex_match = None
            if dex_match:
                dex_name = dex_match.group(1)
            dex_path = f"{tmp_dir}/{dex_name}"

            print_info(f"[apk_analyze] 4) r2 打开 DEX: {dex_path}")
            opened = _call_tool("r2_open_file", {"file_path": dex_path, "auto_analyze": False})
            sids = extract_session_ids(opened)
            if sids:
                ctx.known_sessions.update(sids)
                ctx.active_session_id = sorted(sids)[-1]

            if ctx.active_session_id:
                print_info("[apk_analyze] 5) basic analyze + strings snapshot")
                _call_tool("r2_analyze_target", {"session_id": ctx.active_session_id, "strategy": "basic"})
                _call_tool("r2_run_command", {"session_id": ctx.active_session_id, "command": "i"})
                _call_tool("r2_run_command", {"session_id": ctx.active_session_id, "command": "iz"})
                if analysis_mode == "deep":
                    _call_tool("r2_run_command", {"session_id": ctx.active_session_id, "command": "ic"})
        except (requests.RequestException, JsonRpcError, ValueError, TypeError, OSError, RuntimeError, KeyError) as exc:
            print_info(f"[apk_analyze] 固定取证失败：{exc}（将回退为纯 AI tool_calls 模式）")

        q = (
            f"基于已收集的证据，继续深入分析 APK：{apk_path}。\n"
            "如果已经提取到 dex/拿到 session_id，请从当前 session 继续；"
            "否则请自行用 tool_calls 重新取证。\n"
            "目标：定位校验/反调试/加固逻辑，输出最终 Markdown（## 关键发现/## 证据来源/## 下一步建议）。"
        )
        _run_ai_question(ctx, q, mode="strict")
        return True
    if raw.startswith("dex_analyze "):
        analysis_mode, path = _parse_flag_and_path(raw, "dex_analyze")
        if not path:
            print_info("用法: dex_analyze [--fast|--deep] <dex_path>")
            return True
        try:
            print_info("[dex_analyze] 1) r2 打开 DEX")
            opened = _call_tool("r2_open_file", {"file_path": path, "auto_analyze": False})
            sids = extract_session_ids(opened)
            if sids:
                ctx.known_sessions.update(sids)
                ctx.active_session_id = sorted(sids)[-1]
            if ctx.active_session_id:
                print_info("[dex_analyze] 2) basic analyze + strings/classes snapshot")
                _call_tool("r2_analyze_target", {"session_id": ctx.active_session_id, "strategy": "basic"})
                _call_tool("r2_run_command", {"session_id": ctx.active_session_id, "command": "i"})
                _call_tool("r2_run_command", {"session_id": ctx.active_session_id, "command": "iz"})
                if analysis_mode == "deep":
                    _call_tool("r2_run_command", {"session_id": ctx.active_session_id, "command": "ic"})
        except (requests.RequestException, JsonRpcError, ValueError, TypeError, OSError, RuntimeError, KeyError) as exc:
            print_info(f"[dex_analyze] 固定取证失败：{exc}（将回退为纯 AI tool_calls 模式）")
        q = (
            f"继续深入分析 DEX：{path}\n"
            "目标：定位关键类/关键字符串/验证入口；输出最终 Markdown（## 关键发现/## 证据来源/## 下一步建议）。"
        )
        _run_ai_question(ctx, q, mode="strict")
        return True
    if raw.startswith("so_analyze "):
        analysis_mode, path = _parse_flag_and_path(raw, "so_analyze")
        if not path:
            print_info("用法: so_analyze [--fast|--deep] <so_path>")
            return True
        try:
            print_info("[so_analyze] 1) r2 打开 so")
            opened = _call_tool("r2_open_file", {"file_path": path, "auto_analyze": False})
            sids = extract_session_ids(opened)
            if sids:
                ctx.known_sessions.update(sids)
                ctx.active_session_id = sorted(sids)[-1]
            if ctx.active_session_id:
                print_info("[so_analyze] 2) basic analyze + imports/exports/functions snapshot")
                _call_tool("r2_analyze_target", {"session_id": ctx.active_session_id, "strategy": "basic"})
                _call_tool("r2_run_command", {"session_id": ctx.active_session_id, "command": "i"})
                _call_tool("r2_run_command", {"session_id": ctx.active_session_id, "command": "iI"})
                _call_tool("r2_run_command", {"session_id": ctx.active_session_id, "command": "iE"})
                _call_tool("r2_run_command", {"session_id": ctx.active_session_id, "command": "afl"})
                if analysis_mode == "deep":
                    _call_tool("r2_run_command", {"session_id": ctx.active_session_id, "command": "aaa"})
        except (requests.RequestException, JsonRpcError, ValueError, TypeError, OSError, RuntimeError, KeyError) as exc:
            print_info(f"[so_analyze] 固定取证失败：{exc}（将回退为纯 AI tool_calls 模式）")
        q = (
            f"继续深入分析 native so：{path}\n"
            "目标：优先分析导入导出/JNI/反调试/加密校验迹象；输出最终 Markdown（## 关键发现/## 证据来源/## 下一步建议）。"
        )
        _run_ai_question(ctx, q, mode="strict")
        return True
    return False
