from __future__ import annotations

import json
import re
import time
from typing import Any, Dict, List, Literal, Optional, cast

import requests
from openai.types.chat import ChatCompletionMessageParam, ChatCompletionToolParam

from lib.bridge import JsonRpcError, R2BridgeClient
from lib.client import AIClientSingleton
from lib.config import MAX_CONTEXT_CHARS, MAX_CONTEXT_MESSAGES, MAX_TOOL_RESULT_CHARS
from lib.debug import debug_enabled, debug_log
from lib.debug import read_debug_trace
from lib.schema import extract_mcp_error_text, validate_args
from lib.termux import termux_save_script_wrapper
from lib.ui_core import AdaptiveStreamWriter, RICH_AVAILABLE, UserInterruptError, print_info, print_markdown


def as_msg(value: Dict[str, Any]) -> ChatCompletionMessageParam:
    return cast(ChatCompletionMessageParam, cast(object, value))


def extract_session_ids(obj: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == "session_id" and isinstance(v, str) and v.startswith("session_"):
                found.add(v)
            found.update(extract_session_ids(v))
    elif isinstance(obj, list):
        for item in obj:
            found.update(extract_session_ids(item))
    elif isinstance(obj, str):
        for sid in re.findall(r"session_[A-Za-z0-9_]+", obj):
            found.add(sid)
    return found


class AIAnalyzer:
    @staticmethod
    def _merge_extra_body(req: Dict[str, Any], extra: Dict[str, Any]) -> None:
        if not isinstance(extra, dict) or not extra:
            return
        cur = req.get("extra_body")
        if not isinstance(cur, dict):
            cur = {}
        merged = dict(cur)
        merged.update(extra)
        req["extra_body"] = merged

    @staticmethod
    def _messages_stats(messages: Any) -> Dict[str, int]:
        if not isinstance(messages, list):
            return {"count": 0, "chars": 0}
        total = 0
        for m in messages:
            if not isinstance(m, dict):
                continue
            try:
                total += len(json.dumps(m, ensure_ascii=False))
            except (TypeError, ValueError):
                total += len(str(m.get("content", "") or ""))
        return {"count": len(messages), "chars": total}

    @staticmethod
    def _format_tool_names_for_prompt(tool_specs: Dict[str, Dict[str, Any]], max_chars: int = 1800) -> str:
        names = sorted([k for k in (tool_specs or {}).keys() if isinstance(k, str) and k.strip()])
        if not names:
            return "（当前工具列表为空：未从 tools/list 加载到 schema，或 tool_specs 未传入。）"
        lines: List[str] = [f"（已加载工具 {len(names)} 个；仅展示前若干项）"]
        used = len(lines[0])
        shown = 0
        for n in names:
            line = f"- {n}"
            if used + len(line) + 1 > max_chars:
                break
            lines.append(line)
            used += len(line) + 1
            shown += 1
        if shown < len(names):
            lines.append(f"- ...(其余 {len(names) - shown} 个已省略)")
        return "\n".join(lines)

    @staticmethod
    def _format_tool_required_args_for_prompt(tool_specs: Dict[str, Dict[str, Any]], max_chars: int = 1800) -> str:
        names = sorted([k for k in (tool_specs or {}).keys() if isinstance(k, str) and k.strip()])
        if not names:
            return "（无工具 required 速查表：tool_specs 为空。）"
        lines: List[str] = ["（工具 required 速查表：调用前必须补齐 required）"]
        used = len(lines[0])
        for n in names:
            spec = tool_specs.get(n) if isinstance(tool_specs, dict) else None
            req = []
            if isinstance(spec, dict) and isinstance(spec.get("required"), list):
                req = [str(x) for x in spec.get("required") if isinstance(x, str)]
            suffix = "  [禁用]" if n == "r2_test" else ""
            req_txt = ", ".join(req) if req else "-"
            line = f"- {n}{suffix}: required=[{req_txt}]"
            if used + len(line) + 1 > max_chars:
                lines.append("- ...(其余已省略)")
                break
            lines.append(line)
            used += len(line) + 1
        return "\n".join(lines)

    def __init__(
            self,
            api_key: str,
            model: str,
            base_url: str,
            summary_model: str = "",
            tool_specs: Optional[Dict[str, Dict[str, Any]]] = None,
            kb_path: str = "",
            timeout_s: int = 45,
            client_override: Optional[Any] = None,
            enable_search: bool = False,
            enable_thinking: bool = False,
            max_tool_result_chars: int = MAX_TOOL_RESULT_CHARS,
            max_context_messages: int = MAX_CONTEXT_MESSAGES,
            max_context_chars: int = MAX_CONTEXT_CHARS,
            dangerous_policy: str = "confirm",
            dangerous_allow_regex: str = "",
            dangerous_extra_deny_regex: str = "",
    ):
        self.client = client_override or AIClientSingleton.get_client(api_key, base_url, timeout=timeout_s)
        self.base_url = str(base_url or "")
        self.model = model
        self.summary_model = (summary_model or model).strip() or model
        self.tool_specs = tool_specs or {}
        self.kb_path = kb_path
        self.session_ids: set[str] = set()
        self.last_trace_id = ""
        self.enable_search = bool(enable_search)
        self.enable_thinking = bool(enable_thinking)
        self.max_tool_result_chars = int(max_tool_result_chars)
        self.max_context_messages = int(max_context_messages)
        self.max_context_chars = int(max_context_chars)
        self.dangerous_policy = str(dangerous_policy or "confirm").strip().lower()
        self.dangerous_allow_regex = str(dangerous_allow_regex or "").strip()
        self.dangerous_extra_deny_regex = str(dangerous_extra_deny_regex or "").strip()
        tool_names_hint = self._format_tool_names_for_prompt(self.tool_specs)
        tool_required_hint = self._format_tool_required_args_for_prompt(self.tool_specs)
        self._system_prompt_strict = self._build_system_prompt_strict(tool_names_hint, tool_required_hint)
        self._system_prompt_loose = self._build_system_prompt_loose(tool_names_hint, tool_required_hint)
        self._active_chat_mode: Literal["strict", "loose"] = "loose"
        self.messages: List[ChatCompletionMessageParam] = [
            as_msg(
                {
                    "role": "system",
                    "content": self._system_prompt_loose,
                }
            )
        ]

    def _maybe_enable_web_search(self, req: Dict[str, Any]) -> None:
        if not self.enable_search:
            return
        base = (self.base_url or "").strip().lower()
        if "dashscope.aliyuncs.com" not in base:
            if debug_enabled():
                debug_log("web_search_skipped", {"reason": "base_url_not_dashscope", "base_url": self.base_url})
            return
        self._merge_extra_body(req, {"enable_search": True})
        if debug_enabled():
            debug_log("web_search_enabled", {"base_url": self.base_url})

    def _maybe_enable_deepseek_thinking(self, req: Dict[str, Any]) -> None:
        """
        DeepSeek 思考模式：通过 extra_body.thinking 启用（适用于 deepseek-chat 等模型）。

        参考文档：
        - https://api-docs.deepseek.com/zh-cn/guides/thinking_mode
        """
        if not self.enable_thinking:
            return
        base = (self.base_url or "").strip().lower()
        if "api.deepseek.com" not in base:
            if debug_enabled():
                debug_log("thinking_skipped", {"reason": "base_url_not_deepseek", "base_url": self.base_url})
            return
        if str(self.model or "").strip().lower() == "deepseek-reasoner":
            if debug_enabled():
                debug_log("thinking_skipped", {"reason": "model_is_reasoner", "model": self.model})
            return
        self._merge_extra_body(req, {"thinking": {"type": "enabled"}})
        if debug_enabled():
            debug_log("thinking_enabled", {"base_url": self.base_url, "model": self.model})

    @staticmethod
    def _build_system_prompt_strict(tool_names_hint: str, tool_required_hint: str) -> str:
        return (
            "你是 R2AIBridge（Radare2 + Termux + OS + DB/Log）逆向分析助手。你的目标不是写计划，而是通过“可执行的工具调用”持续取证，直到产出可复现的最终结论（Markdown）。\n"
            "\n"
            "========================\n"
            "0) 硬边界（不可违背）\n"
            "========================\n"
            "0.1 工具与参数\n"
            "- 只能使用 tools/list/schema 中存在的工具名与参数（以本轮注入的工具清单为准）。\n"
            "- 禁止臆造工具名/参数；禁止输出不存在的工具；禁止把工具调用写成文本（必须用标准 tool_calls 结构化调用）。\n"
            "\n"
            "0.2 二进制读取安全\n"
            "- 严禁用 `os_read_file` 读取二进制（.apk/.dex/.so/.db/.png 等）；二进制一律用 `r2_open_file` 分析。\n"
            "- `os_read_file` 仅用于文本（xml/json/yaml/conf/log）。\n"
            "\n"
            "0.3 运行路径/环境\n"
            "- Android/Termux 路径（例如 `/storage/...`、`/data/...`）的操作必须通过 MCP 工具执行：`termux_command` / `r2_*` / `os_*` / `sqlite_query` / `read_logcat`。\n"
            "- 不要输出 “r2>” 之类提示符；不要把交互当成输出的一部分。\n"
            "\n"
            "0.4 DSML 禁止\n"
            "- 禁止在 reasoning_content 或 content 输出任何 DSML/XML/协议文本（如 <｜DSML｜...>）。需要调用工具时只能发 tool_calls。\n"
            "\n"
            "0.5 禁用项\n"
            "- 禁止使用 `r2_test`（除非用户明确要求“诊断/测试 r2 是否可用”）。\n"
            "\n"
            "当前可用工具名（运行时注入，以此为准）：\n"
            f"{tool_names_hint}\n"
            "\n"
            "各工具 required 字段速查（运行时注入）：\n"
            f"{tool_required_hint}\n"
            "\n"
            "=== 参数强制规则（必须遵守）===\n"
            "- 任何 tool_calls 在发出前，必须先对照 tools/list 的 inputSchema：\n"
            "  - `required` 中列出的字段必须全部提供；缺任何一个都禁止调用该工具。\n"
            "  - required 字段若是 string，必须是非空字符串（不能是 \"\" / 空白）。\n"
            "  - 禁止提供 schema 未定义的字段（additionalProperties=false 视为硬约束）。\n"
            "  - JSON 类型必须匹配：string/integer/boolean/object/array 不得混用。\n"
            "- 如果必填参数缺失：\n"
            "  - 优先从上下文/上一轮工具输出中提取补齐；\n"
            "  - 仍无法确定时必须向用户提问获取值；\n"
            "  - 不允许用占位符/猜测值/随便填一个 session_id。\n"
            "\n"
            "========================\n"
            "1) 回合输出合同（最重要）\n"
            "========================\n"
            "你每一轮只能以两种方式结束：\n"
            "\n"
            "A) 继续取证（推荐）\n"
            "- 你必须输出 tool_calls（1~3 个），并且每个 tool_call 前在文字中用一句话说明“目的/期待证据”。\n"
            "- 不允许只说“下一步我将…/继续搜索…/尝试…”，但不发 tool_calls。\n"
            "\n"
            "B) 最终结论（仅在证据充分时）\n"
            "- 你必须输出最终 Markdown，且必须包含三个小节：\n"
            "  - `## 关键发现`（3~8 条）\n"
            "  - `## 证据来源`（逐条对应工具输出/命令）\n"
            "  - `## 下一步建议`（可执行的下一步）\n"
            "\n"
            "只要还需要更多证据，就必须走 A，不允许输出半成品结论。\n"
            "\n"
            "========================\n"
            "2) 核心状态机（强制执行）\n"
            "========================\n"
            "循环执行直到满足“最终结论”：\n"
            "1) 明确子目标：把用户任务拆成 1~3 个可验证子目标（每个子目标都能用工具得到证据）。\n"
            "2) 选择最小工具链：每轮最多 1~3 个工具调用，拿到关键证据。\n"
            "3) 解释证据：用 2~6 行解释“这条输出意味着什么”，并明确下一步要用哪个工具继续取证。\n"
            "4) 记录成果：确认函数用途→`rename_function`；关键结论/密钥/结构体→`add_knowledge_note`。\n"
            "5) 结束条件：只有能写最终 Markdown 时才能停。\n"
            "\n"
            "只要还没到最终结论，继续发 tool_calls，不要输出结束语。\n"
        )

    @staticmethod
    def _build_system_prompt_loose(tool_names_hint: str, tool_required_hint: str) -> str:
        return (
            "你是 R2AIBridge（Radare2 + Termux + OS + DB/Log）助手。\n"
            "你的目标是按用户意图给出最合适的输出：\n"
            "- 用户要求“不要分析/只列清单/只解释概念”时：直接给出简明回答，不要继续思考，也不要强行调用工具。\n"
            "- 用户要求“取证/验证/逆向分析”时：用标准 tool_calls 调用工具获取证据。\n"
            "\n"
            "========================\n"
            "0) 硬边界（不可违背）\n"
            "========================\n"
            "0.1 工具与参数\n"
            "- 只能使用 tools/list/schema 中存在的工具名与参数（以本轮注入的工具清单为准）。\n"
            "- 禁止臆造工具名/参数；禁止输出不存在的工具；禁止把工具调用写成文本（必须用标准 tool_calls 结构化调用）。\n"
            "\n"
            "0.2 二进制读取安全\n"
            "- 严禁用 `os_read_file` 读取二进制（.apk/.dex/.so/.db/.png 等）；二进制一律用 `r2_open_file` 分析。\n"
            "- `os_read_file` 仅用于文本（xml/json/yaml/conf/log）。\n"
            "\n"
            "0.3 运行路径/环境\n"
            "- Android/Termux 路径（例如 `/storage/...`、`/data/...`）的操作必须通过 MCP 工具执行：`termux_command` / `r2_*` / `os_*` / `sqlite_query` / `read_logcat`。\n"
            "- 不要输出 “r2>” 之类提示符；不要把交互当成输出的一部分。\n"
            "\n"
            "0.4 DSML 禁止\n"
            "- 禁止在 reasoning_content 或 content 输出任何 DSML/XML/协议文本（如 <｜DSML｜...>）。需要调用工具时只能发 tool_calls。\n"
            "\n"
            "0.5 禁用项\n"
            "- 禁止使用 `r2_test`（除非用户明确要求“诊断/测试 r2 是否可用”）。\n"
            "\n"
            "当前可用工具名（运行时注入，以此为准）：\n"
            f"{tool_names_hint}\n"
            "\n"
            "各工具 required 字段速查（运行时注入）：\n"
            f"{tool_required_hint}\n"
            "\n"
            "=== 参数强制规则（必须遵守）===\n"
            "- 任何 tool_calls 在发出前，必须先对照 tools/list 的 inputSchema：\n"
            "  - `required` 中列出的字段必须全部提供；缺任何一个都禁止调用该工具。\n"
            "  - required 字段若是 string，必须是非空字符串（不能是 \"\" / 空白）。\n"
            "  - 禁止提供 schema 未定义的字段（additionalProperties=false 视为硬约束）。\n"
            "  - JSON 类型必须匹配：string/integer/boolean/object/array 不得混用。\n"
            "- 如果必填参数缺失：必须向用户提问获取值；不允许用占位符/猜测值。\n"
            "\n"
            "========================\n"
            "1) 输出约定\n"
            "========================\n"
            "- 允许直接输出回答（例如工具清单/概念解释/步骤说明）。\n"
            "- 只有当需要真实取证/执行命令时，才输出 tool_calls。\n"
            "- 若用户明确要求“最终报告”，再输出 Markdown（可包含：## 关键发现/## 证据来源/## 下一步建议）。\n"
        )

    def _ensure_system_prompt_for_mode(self, mode: Literal["strict", "loose"]) -> None:
        if mode == self._active_chat_mode:
            return
        self._active_chat_mode = mode
        tool_names_hint = self._format_tool_names_for_prompt(self.tool_specs)
        tool_required_hint = self._format_tool_required_args_for_prompt(self.tool_specs)
        self._system_prompt_strict = self._build_system_prompt_strict(tool_names_hint, tool_required_hint)
        self._system_prompt_loose = self._build_system_prompt_loose(tool_names_hint, tool_required_hint)
        if isinstance(self.messages, list) and self.messages:
            sys0 = self.messages[0]
            if isinstance(sys0, dict) and sys0.get("role") == "system":
                sys0["content"] = self._system_prompt_strict if mode == "strict" else self._system_prompt_loose

    def reset(self) -> None:
        self.messages = [self.messages[0]]
        self.session_ids.clear()

    def export_session(self) -> Dict[str, Any]:
        return {"model": self.model, "summary_model": self.summary_model, "messages": self.messages}

    def load_session(self, session_data: Dict[str, Any]) -> bool:
        raw = session_data.get("messages")
        if not isinstance(raw, list) or not raw:
            return False
        for msg in raw:
            if not isinstance(msg, dict):
                return False
        self.messages = [as_msg(msg) for msg in raw]
        model = session_data.get("model")
        if isinstance(model, str) and model.strip():
            self.model = model.strip()
        summary_model = session_data.get("summary_model")
        if isinstance(summary_model, str) and summary_model.strip():
            self.summary_model = summary_model.strip()
        return True

    def _trim_messages(self) -> None:
        before = self._messages_stats(self.messages) if debug_enabled() else None
        try:
            self._sanitize_messages_for_tools()
        except (TypeError, ValueError, KeyError) as exc:
            if debug_enabled():
                debug_log("trim_sanitize_error", {"error": f"{type(exc).__name__}: {str(exc)[:200]}"})

        if len(self.messages) > self.max_context_messages:
            system_msg = self.messages[0]
            tail = self.messages[-(self.max_context_messages - 1):]

            while tail and isinstance(tail[0], dict) and tail[0].get("role") == "tool":
                tail = tail[1:]

            self.messages = [system_msg] + tail

        try:
            total_chars = 0

            def _msg_size(msg_obj: Any) -> int:
                if not isinstance(msg_obj, dict):
                    return 0
                try:
                    return len(json.dumps(msg_obj, ensure_ascii=False))
                except (TypeError, ValueError):
                    return len(str(msg_obj.get("content", "") or ""))

            for m in self.messages:
                total_chars += _msg_size(m)
            if total_chars <= self.max_context_chars:
                return

            msgs = self.messages
            if not msgs:
                return

            system = msgs[:1]
            rest = msgs[1:]

            blocks: List[List[ChatCompletionMessageParam]] = []
            i = 0
            while i < len(rest):
                m0 = rest[i]
                if not isinstance(m0, dict):
                    i += 1
                    continue
                role = str(m0.get("role", "") or "")
                if role == "assistant" and isinstance(m0.get("tool_calls"), list) and m0.get("tool_calls"):
                    block: List[ChatCompletionMessageParam] = [as_msg(m0)]
                    i += 1
                    while i < len(rest):
                        tm0 = rest[i]
                        if not isinstance(tm0, dict):
                            break
                        t_role = str(tm0.get("role", "") or "")
                        if t_role != "tool":
                            break
                        block.append(as_msg(tm0))
                        i += 1
                    blocks.append(block)
                    continue
                blocks.append([as_msg(m0)])
                i += 1

            kept: List[ChatCompletionMessageParam] = []
            kept_chars = _msg_size(system[0]) if system else 0
            for block in reversed(blocks):
                bsz = sum(_msg_size(x) for x in block)
                if kept and (kept_chars + bsz > self.max_context_chars):
                    break
                kept.extend(reversed(block))
                kept_chars += bsz

            kept.reverse()
            self.messages = system + kept

            if len(self.messages) >= 2 and isinstance(self.messages[1], dict) and self.messages[1].get(
                    "role") == "tool":
                tail2 = self.messages[1:]
                while tail2 and isinstance(tail2[0], dict) and tail2[0].get("role") == "tool":
                    tail2 = tail2[1:]
                self.messages = self.messages[:1] + tail2
        except (TypeError, ValueError, KeyError) as exc:
            if debug_enabled():
                debug_log("trim_char_budget_error", {"error": f"{type(exc).__name__}: {str(exc)[:200]}"})
            return
        finally:
            if before is not None:
                after = self._messages_stats(self.messages)
                if before != after:
                    debug_log("trim_messages", {"before": before, "after": after})

    @staticmethod
    def _contains_dsml_markup(text: str) -> bool:
        t = text or ""
        return (
                ("<｜DSML｜" in t)
                or ("<|DSML|" in t)
                or ("</｜DSML｜" in t)
                or ("</|DSML|" in t)
                or (re.search(r"<[|｜]DSML[|｜](invoke|parameter)\b", t, flags=re.IGNORECASE) is not None)
        )

    @staticmethod
    def _parse_dsml_function_calls(text: str) -> List[Dict[str, Any]]:
        content = (text or "").strip()
        if not content:
            return []
        invoke_pattern = re.compile(
            r'<[|｜]DSML[|｜]invoke\s+name="([^"]+)"\s*>([\s\S]*?)</[|｜]DSML[|｜]invoke>',
            flags=re.IGNORECASE,
        )
        param_pattern = re.compile(
            r'<[|｜]DSML[|｜]parameter\s+name="([^"]+)"[^>]*>([\s\S]*?)</[|｜]DSML[|｜]parameter>',
            flags=re.IGNORECASE,
        )
        calls: List[Dict[str, Any]] = []
        for m in invoke_pattern.finditer(content):
            tool = m.group(1).strip()
            body = m.group(2)
            args: Dict[str, Any] = {}
            for pm in param_pattern.finditer(body):
                args[pm.group(1).strip()] = pm.group(2).strip()
            calls.append(
                {
                    "id": f"dsml_{len(calls)}",
                    "type": "function",
                    "function": {"name": tool, "arguments": json.dumps(args, ensure_ascii=False)},
                }
            )
        if calls:
            return calls

        if "DSML" not in content and "dsml" not in content:
            return []
        invoke_name_pattern = re.compile(r'<[|｜]DSML[|｜]invoke[^>]*\sname="([^"]+)"', flags=re.IGNORECASE)
        name_match = invoke_name_pattern.search(content)
        if not name_match:
            return []
        tool = name_match.group(1).strip()
        args: Dict[str, Any] = {}
        for pm in param_pattern.finditer(content):
            args[pm.group(1).strip()] = pm.group(2).strip()
        return [
            {
                "id": "dsml_0",
                "type": "function",
                "function": {"name": tool, "arguments": json.dumps(args, ensure_ascii=False)},
            }
        ]

    def _looks_like_missing_tool_call(self, content: str) -> bool:
        text = (content or "").strip()
        if not text:
            return False
        if ("## 关键发现" in text) and ("## 证据来源" in text) and ("## 下一步建议" in text):
            return False
        compact = re.sub(r"\s+", "", text)
        if re.search(r"\b(termux_command|r2_[A-Za-z0-9_]+|os_[A-Za-z0-9_]+)\b", text):
            return True
        if re.search(r"(termux_command|r2_[A-Za-z0-9_]+|os_[A-Za-z0-9_]+)", compact):
            return True
        for name in self.tool_specs.keys():
            if not name:
                continue
            if name in text or name in compact:
                return True
        if "工具调用" in text or ("使用" in text and "工具" in text) or ("调用" in text and "工具" in text):
            return True
        if re.search(
                r"\b(unzip|zipinfo|aapt2?|jadx|apktool|baksmali|smali|dexdump|dex2jar|readelf|objdump|nm|strings|binwalk|dd|grep|head|tail|sed|awk|cut|sort|wc)\b",
                text,
                flags=re.IGNORECASE,
        ):
            return True
        if re.search(r"\|\s*(head|tail|grep|rg)\b", text, flags=re.IGNORECASE):
            return True
        if re.search(r"(用|通过)\s*`[^`]+`.*(查看|运行|执行|调用|检查|安装|列出|解压|提取|搜索|查找|打开|分析)", text):
            return True
        if re.search(r"使用\s*`[^`]+`.*(运行|执行|调用|检查|安装|列出|解压|提取|搜索|查找|打开|分析)", text):
            return True
        if re.search(r"(查看|运行|执行|调用|检查|安装|列出|解压|提取|搜索|查找|打开|分析)\s*`[^`]+`", text):
            return True
        if re.search(r"(下一步|接下来|让我|先|然后|尝试|继续)", text) and re.search(
                r"(运行|执行|调用|检查|安装|列出|解压|提取|搜索|查找|打开|加载|分析|反编译|反汇编)",
                text,
        ):
            return True
        if re.search(r"搜索\s*[\"“”']?[^\"“”'\n]{1,40}[\"“”']?", text) and re.search(
                r"(继续|再|然后|下一步|接下来|让我)", text):
            return True
        if "`" in text and re.search(
                r"(下一步|接下来|让我|先|然后|尝试|继续).*(运行|执行|调用|检查|安装|列出|解压|提取|搜索|打开|分析)",
                text):
            return True
        return False

    @staticmethod
    def _merge_delta_tool_calls(delta_tool_calls: List[Any], tool_calls_acc: List[Dict[str, Any]]) -> None:
        for tc in delta_tool_calls:
            idx = tc.index if tc.index is not None else 0
            while len(tool_calls_acc) <= idx:
                tool_calls_acc.append({"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
            item = tool_calls_acc[idx]
            if tc.id:
                item["id"] = tc.id
            fn = getattr(tc, "function", None)
            if fn is not None:
                if fn.name:
                    item["function"]["name"] = fn.name
                if fn.arguments:
                    item["function"]["arguments"] += fn.arguments

    def _sanitize_messages_for_tools(self) -> None:
        """
        保证发送给 API 的 messages 满足 tool_calls 协议：
        - tool 消息必须紧跟在触发它的 assistant(tool_calls) 后
        - assistant.tool_calls 里的每个 id 必须有对应 tool(tool_call_id=id)

        这一步是“协议级兜底”，用于修复：中断/裁剪/异常导致的 tool_calls/tool 不配对，避免 400。
        """
        src = [m for m in self.messages if isinstance(m, dict)]
        cleaned: List[ChatCompletionMessageParam] = []
        i = 0
        while i < len(src):
            msg = src[i]
            role = str(msg.get("role", "") or "")
            if role != "assistant":
                # tool 不能独立存在（必须跟随 assistant.tool_calls）；孤儿 tool 丢弃
                if role != "tool":
                    cleaned.append(as_msg(msg))
                i += 1
                continue

            # assistant
            tcs_raw = msg.get("tool_calls")
            if not (isinstance(tcs_raw, list) and tcs_raw):
                cleaned.append(as_msg(msg))
                i += 1
                continue

            expected_ids: List[str] = []
            fixed_tcs: List[Dict[str, Any]] = []
            for idx, tc in enumerate(tcs_raw):
                if not isinstance(tc, dict):
                    continue
                fn = tc.get("function")
                name = str(fn.get("name", "") or "").strip() if isinstance(fn, dict) else ""
                tid = str(tc.get("id", "") or "").strip()
                if (not tid) and name:
                    tid = f"fixup_{len(cleaned)}_{idx}"
                    tc["id"] = tid
                if not tid or not name:
                    continue
                expected_ids.append(tid)
                fixed_tcs.append(tc)

            j = i + 1
            tool_msgs: List[Dict[str, Any]] = []
            responded: set[str] = set()
            while j < len(src):
                tm = src[j]
                t_role = str(tm.get("role", "") or "")
                if t_role != "tool":
                    break
                tci = str(tm.get("tool_call_id", "") or "").strip()
                if tci and (tci in expected_ids):
                    tool_msgs.append(tm)
                    responded.add(tci)
                j += 1

            filtered_tcs = [tc for tc in fixed_tcs if str(tc.get("id", "") or "").strip() in responded]
            am_out = dict(msg)
            if filtered_tcs:
                am_out["tool_calls"] = filtered_tcs
            else:
                am_out.pop("tool_calls", None)
                c = str(am_out.get("content", "") or "").strip()
                r = str(am_out.get("reasoning_content", "") or "").strip()
                if (not c) and (not r):
                    i = j
                    continue

            cleaned.append(as_msg(am_out))
            for tm in tool_msgs:
                cleaned.append(as_msg(tm))
            i = j

        self.messages = cleaned

    @staticmethod
    def _parse_tool_arguments(tc: Dict[str, Any]) -> Dict[str, Any]:
        try:
            raw = tc.get("function", {}).get("arguments") if isinstance(tc.get("function"), dict) else None
            args = json.loads(raw or "{}")
            if not isinstance(args, dict):
                raise ValueError("参数必须是对象")
            return args
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            return {"_parse_error": str(exc)}

    @staticmethod
    def _compact_text_output(
            text: str,
            *,
            head_lines: int = 40,
            tail_lines: int = 80,
            max_chars: int = 12_000,
    ) -> str:
        t = (text or "").replace("\r\n", "\n").replace("\r", "\n")
        if not t:
            return t
        lines = t.split("\n")
        if len(lines) > (head_lines + tail_lines + 10):
            head = lines[:head_lines]
            tail = lines[-tail_lines:]
            omitted = max(0, len(lines) - len(head) - len(tail))
            out = "\n".join(head) + f"\n...(中间省略 {omitted} 行)...\n" + "\n".join(tail)
            if len(out) <= max_chars:
                return out
            t = out
        if len(t) > max_chars:
            keep_head = int(max_chars * 0.65)
            keep_tail = max_chars - keep_head
            return (
                    t[:keep_head]
                    + f"\n...(输出过长已截断：总长 {len(t)} 字符；保留头 {keep_head} + 尾 {keep_tail})...\n"
                    + (t[-keep_tail:] if keep_tail > 0 else "")
            )
        return t

    def _compact_tool_result(self, tool_name: str, result: Any) -> Any:
        if not isinstance(result, dict):
            return result
        out: Dict[str, Any] = dict(result)

        if tool_name == "termux_command":
            key_re = re.compile(r"(traceback|exception|error|fatal|failed|permission denied|no such file|not found)",
                                re.IGNORECASE)
            for k in ("stdout", "stderr", "output", "text", "result"):
                v = out.get(k)
                if isinstance(v, str) and len(v) > 2000:
                    lines = v.replace("\r\n", "\n").replace("\r", "\n").split("\n")
                    hit_idx: list[int] = []
                    for i, line in enumerate(lines):
                        if key_re.search(line or ""):
                            hit_idx.append(i)
                            if len(hit_idx) >= 20:
                                break
                    if hit_idx:
                        keep: set[int] = set()
                        for i in hit_idx:
                            for j in range(max(0, i - 3), min(len(lines), i + 4)):
                                keep.add(j)
                        picked = [lines[i] for i in sorted(keep)]
                        excerpt = "\n".join(picked).strip()
                        out[k] = self._compact_text_output(excerpt, head_lines=80, tail_lines=80, max_chars=12_000)
                    else:
                        out[k] = self._compact_text_output(v)

        for k, v in list(out.items()):
            if isinstance(v, str) and len(v) > 20_000:
                out[k] = self._compact_text_output(v, max_chars=12_000)

        raw = out.get("raw")
        if isinstance(raw, dict):
            raw2 = dict(raw)
            for k, v in list(raw2.items()):
                if isinstance(v, str) and len(v) > 20_000:
                    raw2[k] = self._compact_text_output(v, max_chars=12_000)
            out["raw"] = raw2
        return out

    @staticmethod
    def _is_dangerous_termux_command(cmd: str) -> tuple[bool, str]:
        c = (cmd or "").strip()
        if not c:
            return False, ""
        low = c.lower()

        patterns: list[tuple[str, str]] = [
            (r"\brm\s+-rf?\b", "rm -rf"),
            (r"\brm\s+-r\b", "rm -r"),
            (r"\bmkfs(\.|_|\s)", "mkfs"),
            (r"\bdd\s+if=", "dd if="),
            (r"\bdd\s+of=", "dd of="),
            (r"\bshutdown\b|\breboot\b", "shutdown/reboot"),
            (r"\bmount\b|\bumount\b", "mount/umount"),
            (r"\bchmod\b.*\s/($|\s)", "chmod on /"),
            (r"\bchown\b.*\s/($|\s)", "chown on /"),
            (r"(curl|wget).*\|\s*(sh|bash)\b", "curl|sh / wget|sh"),
            (r">\s*/dev/block/", "write to /dev/block"),
        ]
        for pat, reason in patterns:
            if re.search(pat, low):
                return True, reason
        return False, ""

    def _dangerous_action_for_termux_command(self, cmd: str) -> tuple[bool, str]:
        is_danger, reason = self._is_dangerous_termux_command(cmd)
        if self.dangerous_extra_deny_regex:
            try:
                if re.search(self.dangerous_extra_deny_regex, cmd or "", flags=re.IGNORECASE):
                    return True, reason or "extra_deny_regex"
            except re.error:
                pass
        if self.dangerous_allow_regex:
            try:
                if re.search(self.dangerous_allow_regex, cmd or "", flags=re.IGNORECASE):
                    return False, ""
            except re.error:
                pass
        return is_danger, reason

    @staticmethod
    def is_dangerous_termux_command(cmd: str) -> tuple[bool, str]:
        return AIAnalyzer._is_dangerous_termux_command(cmd)

    def dangerous_action_for_termux_command(self, cmd: str) -> tuple[bool, str]:
        return self._dangerous_action_for_termux_command(cmd)

    def _recoverable_guidance(self, recoverable_errors: List[str]) -> str:
        blob = "\n".join(str(x) for x in (recoverable_errors or [])).lower()
        if not blob.strip():
            return ""
        hints: List[str] = []

        if re.search(r"(no such file|enoent|not found|does not exist|file not found)", blob):
            hints.append("路径/文件不存在：先用 termux_command 执行 pwd + ls/stat 校验真实路径，再重试相关 r2_* 工具。")
        if re.search(r"(permission denied|eacces|operation not permitted)", blob):
            hints.append(
                "权限问题：优先改用可读目录(/storage/emulated/0/... 或 $HOME)，必要时提示用户 termux-setup-storage/chmod。")
        if "session" in blob and re.search(r"(invalid|not found|closed|expired)", blob):
            hints.append("session 无效：先重新 r2_open_file 获取新 session_id，再继续 r2_run_command/r2_analyze_target。")
        if re.search(r"(timeout|timed out|connection|temporar|gateway|502|503|504)", blob):
            hints.append("网络/网关抖动：优先重试同一 tool_calls；必要时先 health，再继续 tools/call。")
        if re.search(r"(invalid json|json|decode)", blob) and "http" in blob:
            hints.append("服务端返回非 JSON：检查 bridge/服务端日志，或先 health 确认服务端状态后再重试。")

        if self.session_ids:
            sids = ", ".join(sorted(list(self.session_ids))[:4])
            hints.append(f"当前已知可用 session_id（如需复用）：{sids}")

        if not hints:
            return ""
        return "建议修复动作：\n" + "\n".join(f"- {h}" for h in hints[:6])

    @staticmethod
    def _nonrecoverable_guidance(errors: List[str]) -> str:
        blob = "\n".join(str(x) for x in (errors or [])).lower()
        if not blob.strip():
            return ""
        hints: List[str] = []

        if re.search(r"(401|403|unauthorized|forbidden|api key|invalid key|auth)", blob):
            hints.append("鉴权失败：检查 AI API Key / Base URL 是否正确；确认服务端/代理未拦截。")
        if re.search(r"(429|rate limit|too many requests)", blob):
            hints.append("触发限流：降低并发/等待一会再试；必要时更换模型或提高限额。")
        if re.search(r"(500|internal server error)", blob) or re.search(r"(502|503|504|gateway)", blob):
            hints.append("服务端/网关异常：先执行 health 确认桥接服务可用；再重试 tools/list 或 tool_calls。")
        if re.search(r"(schema|参数校验失败|required|additionalproperties)", blob):
            hints.append("参数/schema 问题：先运行 list/tools 确认 inputSchema，再按 required 字段补齐参数。")
        if re.search(r"(permission denied|eacces|operation not permitted)", blob):
            hints.append("权限问题：确认 Termux 存储权限（termux-setup-storage）或改用可读路径 /storage/emulated/0/...。")
        if re.search(r"(no such file|enoent|not found|does not exist)", blob):
            hints.append("路径不存在：先用 termux_command 执行 ls/stat/pwd 确认路径，再用正确路径重试。")

        if not hints:
            return ""
        return "可能原因与建议：\n" + "\n".join(f"- {h}" for h in hints[:6])

    def _build_recoverable_prompt(
            self,
            *,
            success_tool_names: List[str],
            recoverable_errors: List[str],
            partial_success: bool,
    ) -> str:
        ok_list = ", ".join((success_tool_names or [])[:8]) if success_tool_names else "(无)"
        guidance = self._recoverable_guidance(recoverable_errors)
        err_lines = "\n".join(f"- {e}" for e in (recoverable_errors or [])[:8])

        if partial_success:
            return (
                    "你已经成功执行了部分工具调用；请不要重复调用已成功的工具（除非你能明确说明必须重复的理由）。\n"
                    f"本轮已成功工具（不要重复）：{ok_list}\n"
                    "以下为本轮可恢复失败（请仅针对这些失败项修复并重发 tool_calls）：\n"
                    + err_lines
                    + (("\n\n" + guidance) if guidance else "")
                    + "\n\n"
                      "要求：直接输出标准 tool_calls；不要输出 DSML；不要输出口头计划；不要输出最终 Markdown。\n"
            )

        return (
                "你刚才的工具调用返回了“可恢复(recoverable=true)”错误，说明还没完成任务。\n"
                "请不要输出最终 Markdown/结论；请直接修正并重发标准 tool_calls（只输出 tool_calls）。\n"
                "本轮可恢复错误摘要：\n"
                + "\n".join(f"- {e}" for e in (recoverable_errors or [])[:6])
                + (("\n\n" + guidance) if guidance else "")
                + "\n\n"
                  "硬要求：如果你需要 session_id，必须先从上一轮工具输出中提取或重新 r2_open_file；禁止凭空猜测。\n"
        )

    def _stream_assistant_turn(self, tool_choice: str = "auto") -> Dict[str, Any]:
        self._trim_messages()
        if debug_enabled():
            debug_log("model_request", {"tool_choice": tool_choice, "messages": self._messages_stats(self.messages)})
        req: Dict[str, Any] = {"model": self.model, "messages": self.messages, "tool_choice": tool_choice,
                               "stream": True}
        self._maybe_enable_web_search(req)
        self._maybe_enable_deepseek_thinking(req)
        if tool_choice != "none":
            req["tools"] = [
                ChatCompletionToolParam(
                    type="function",
                    function={
                        "name": k,
                        "description": f"R2 MCP 工具: {k}",
                        "parameters": {"type": "object", "properties": v["properties"], "required": v["required"],
                                       "additionalProperties": False},
                    },
                )
                for k, v in self.tool_specs.items()
            ]
        msg: Dict[str, Any] = {"role": "assistant", "content": "", "reasoning_content": ""}
        tool_calls: List[Dict[str, Any]] = []
        writer = AdaptiveStreamWriter()
        started = False
        thinking_started = False
        reasoning = ""
        raw_content = ""
        streamed_markdown = False
        suppress_answer_output = False
        answer_buffer = ""
        buffer_limit = 48
        last_finish_reason: str = ""
        _stream_err_list: list[type[BaseException]] = [TimeoutError]
        try:
            import httpx as _httpx  # type: ignore[import-not-found]

            _stream_err_list.extend([_httpx.TimeoutException, _httpx.ReadTimeout])
        except ImportError:
            pass
        try:
            import httpcore as _httpcore  # type: ignore[import-not-found]

            _stream_err_list.append(_httpcore.ReadTimeout)
        except ImportError:
            pass
        try:
            import openai as _openai  # type: ignore[import-not-found]

            for _n in [
                "APIConnectionError",
                "APITimeoutError",
                "RateLimitError",
                "APIStatusError",
                "APIError",
                "BadRequestError",
                "AuthenticationError",
                "PermissionDeniedError",
                "InternalServerError",
            ]:
                _cls = getattr(_openai, _n, None)
                if isinstance(_cls, type) and issubclass(_cls, BaseException):
                    _stream_err_list.append(_cls)
        except ImportError:
            pass
        _stream_errs: tuple[type[BaseException], ...] = tuple(_stream_err_list)

        try:
            stream = self.client.chat.completions.create(**req)
        except _stream_errs as exc:
            raise RuntimeError(f"AI 请求失败（连接/超时/服务端错误）: {type(exc).__name__}: {exc}") from exc

        try:
            for chunk in stream:
                if not chunk.choices:
                    continue
                fr = getattr(chunk.choices[0], "finish_reason", None)
                if isinstance(fr, str) and fr:
                    last_finish_reason = fr
                delta = chunk.choices[0].delta
                r = getattr(delta, "reasoning_content", None)
                if r:
                    if not thinking_started:
                        writer.write_prefix("[思考] ")
                        thinking_started = True
                    writer.write(r)
                    reasoning += r
                c = getattr(delta, "content", None)
                if c:
                    raw_content += c
                    if not suppress_answer_output:
                        if not started:
                            answer_buffer += c
                            buf = answer_buffer
                            if self._contains_dsml_markup(buf) and buf.lstrip().startswith("<"):
                                suppress_answer_output = True
                                answer_buffer = ""
                            elif ("\n" in buf) or (len(buf) >= buffer_limit) or (not buf.lstrip().startswith("<")):
                                writer.write_prefix("[回答] ")
                                started = True
                                head = (buf or "").lstrip()
                                looks_md = bool(
                                    head.startswith("#")
                                    or head.startswith("```")
                                    or head.startswith(">")
                                    or re.search(r"(?m)^\s*[-*]\s+\S+", buf) is not None
                                    or re.search(r"(?m)^\s*\d+\.\s+\S+", buf) is not None
                                )
                                if looks_md and writer.enable_markdown_stream():
                                    writer.newline()
                                    streamed_markdown = True
                                writer.write(buf)
                                msg["content"] = msg.get("content", "") + buf
                                answer_buffer = ""
                        else:
                            if self._contains_dsml_markup(c):
                                suppress_answer_output = True
                            else:
                                writer.write(c)
                                msg["content"] = msg.get("content", "") + c
                self._merge_delta_tool_calls(getattr(delta, "tool_calls", None) or [], tool_calls)
        except KeyboardInterrupt as exc:
            close_fn = getattr(stream, "close", None)
            if callable(close_fn):
                close_fn()
            writer.newline()
            raise UserInterruptError("用户中断了当前 AI 输出") from exc
        except _stream_errs as exc:
            close_fn = getattr(stream, "close", None)
            if callable(close_fn):
                close_fn()
            try:
                writer.stop_markdown_stream()
            except (ModuleNotFoundError, ImportError, TypeError, ValueError, OSError, AttributeError):
                pass
            writer.newline()
            raise RuntimeError(f"AI 流式请求失败（超时/连接问题）: {type(exc).__name__}: {exc}") from exc
        if (not suppress_answer_output) and (not started) and answer_buffer:
            writer.write_prefix("[回答] ")
            started = True
            head = (answer_buffer or "").lstrip()
            looks_md = bool(
                head.startswith("#")
                or head.startswith("```")
                or head.startswith(">")
                or re.search(r"(?m)^\s*[-*]\s+\S+", answer_buffer) is not None
                or re.search(r"(?m)^\s*\d+\.\s+\S+", answer_buffer) is not None
            )
            if looks_md and writer.enable_markdown_stream():
                writer.newline()
                streamed_markdown = True
            writer.write(answer_buffer)
            msg["content"] = msg.get("content", "") + answer_buffer

        writer.stop_markdown_stream()
        if thinking_started or started:
            writer.newline()
        msg["reasoning_content"] = reasoning
        msg["_finish_reason"] = last_finish_reason
        msg["_answer_started"] = started
        msg["_thinking_started"] = thinking_started
        msg["_raw_content"] = raw_content
        msg["_streamed_markdown"] = streamed_markdown
        if tool_calls:
            msg["tool_calls"] = tool_calls
        return msg

    def chat(self, user_text: str, bridge: R2BridgeClient, mode: str = "loose") -> str:
        def _to_history_msg(am: Dict[str, Any]) -> Dict[str, Any]:
            return {k: v for k, v in am.items() if not str(k).startswith("_")}

        def _looks_like_final_markdown(md: str) -> bool:
            t = (md or "").replace("\r\n", "\n").replace("\r", "\n").strip()
            if not t:
                return False
            required = ["## 关键发现", "## 证据来源", "## 下一步建议"]
            if not all(x in t for x in required):
                return False

            def _section(section_name: str) -> str:
                match = re.search(rf"(?ms)^##\s+{re.escape(section_name)}\s*$\n(.*?)(?=^##\s+|\Z)", t)
                return (match.group(1) if match else "").strip()

            s1 = _section("关键发现")
            s2 = _section("证据来源")
            s3 = _section("下一步建议")
            if len(s1) < 20 or len(s2) < 20 or len(s3) < 20:
                return False
            if not (("session_" in s2) or ("r2_" in s2) or ("termux" in s2) or ("工具" in s2) or ("命令" in s2)):
                return False
            return True

        def _looks_like_markdown(md: str) -> bool:
            t = (md or "").replace("\r\n", "\n").replace("\r", "\n").lstrip()
            if not t:
                return False
            if t.startswith("#") or t.startswith("```") or t.startswith(">"):
                return True
            if re.search(r"(?m)^\s*[-*]\s+\S+", t):
                return True
            if re.search(r"(?m)^\s*\d+\.\s+\S+", t):
                return True
            return False

        def _append_key_commands(final_md: str, trace_id_str: str) -> str:
            if not trace_id_str:
                return final_md
            events = read_debug_trace(trace_id_str, max_events=2000)
            tools: list[str] = []
            for rec in events:
                if not isinstance(rec, dict):
                    continue
                if rec.get("event") != "tool_call":
                    continue
                data = rec.get("data")
                if not isinstance(data, dict):
                    continue
                tool_name_str = str(data.get("tool_name", "") or "").strip()
                if tool_name_str and (tool_name_str not in tools):
                    tools.append(tool_name_str)
                if len(tools) >= 30:
                    break
            if not tools:
                return final_md
            block = "## 关键命令清单\n" + "\n".join(f"- {tool}" for tool in tools) + "\n"
            return (final_md.rstrip() + "\n\n" + block).strip() + "\n"

        mode_norm = str(mode or "").strip().lower()
        chat_mode: Literal["strict", "loose"] = "strict" if mode_norm == "strict" else "loose"
        self._ensure_system_prompt_for_mode(chat_mode)

        self.messages.append(as_msg({"role": "user", "content": user_text}))
        self._trim_messages()
        errors: List[str] = []
        missing_tool_retry = 0
        max_missing_tool_retry = 6
        max_turns = 24
        bad_validation_retry = 0
        max_bad_validation_retry = 4
        recoverable_retry = 0
        max_recoverable_retry = 4
        recoverable_hint_retry = 0
        max_recoverable_hint_retry = 6
        trace_id = f"tr_{int(time.time() * 1000)}"
        self.last_trace_id = trace_id
        turn_id = 0
        strict_mode = (chat_mode == "strict")
        last_nonempty_answer: str = ""
        for _ in range(max_turns):
            turn_id += 1
            assistant_msg = self._stream_assistant_turn(tool_choice="auto")
            finish_reason = str(assistant_msg.get("_finish_reason") or "").strip().lower()
            answer_started = bool(assistant_msg.get("_answer_started") is True)
            tool_calls = assistant_msg.get("tool_calls", [])
            try:
                cur = str(assistant_msg.get("content") or "").strip()
                if cur:
                    last_nonempty_answer = cur
            except (TypeError, ValueError):
                pass
            if debug_enabled():
                names: List[str] = []
                if isinstance(tool_calls, list):
                    for tc in tool_calls:
                        if not isinstance(tc, dict):
                            continue
                        fn = tc.get("function")
                        if isinstance(fn, dict):
                            n = str(fn.get("name", "") or "").strip()
                            if n:
                                names.append(n)
                debug_log(
                    "assistant_turn",
                    {
                        "trace_id": trace_id,
                        "turn_id": turn_id,
                        "mode": chat_mode,
                        "finish_reason": finish_reason,
                        "answer_started": answer_started,
                        "tool_calls_count": len(tool_calls) if isinstance(tool_calls, list) else 0,
                        "tool_call_names": names[:20],
                    },
                )
            if isinstance(tool_calls, list) and tool_calls:
                filtered: List[Dict[str, Any]] = []
                for tc in tool_calls:
                    if not isinstance(tc, dict):
                        continue
                    fn = tc.get("function")
                    name = ""
                    if isinstance(fn, dict):
                        name = str(fn.get("name", "") or "").strip()
                    if not name:
                        continue
                    tid = str(tc.get("id", "") or "").strip()
                    if not tid:
                        tc["id"] = f"tc_{int(time.time() * 1000)}_{len(filtered)}"
                    filtered.append(tc)
                if not filtered:
                    assistant_msg.pop("tool_calls", None)
                tool_calls = filtered
            if not tool_calls:
                dsml_src = (
                        str(assistant_msg.get("_raw_content", "") or "")
                        + "\n"
                        + str(assistant_msg.get("reasoning_content", "") or "")
                ).strip()
                dsml_calls = self._parse_dsml_function_calls(dsml_src)
                if dsml_calls:
                    print_info("[提示] 检测到 DSML 工具调用文本，已自动转换为 tool_calls 并继续执行。")
                    assistant_msg["tool_calls"] = dsml_calls
                    tool_calls = dsml_calls
                    assistant_msg["content"] = ""
            if not tool_calls:
                content = assistant_msg.get("content") or ""
                reasoning_text = assistant_msg.get("reasoning_content") or ""
                raw_text = assistant_msg.get("_raw_content")
                combined = (str(raw_text if isinstance(raw_text, str) else content) + "\n" + str(
                    reasoning_text)).strip()
                no_tool_instruction = (
                    "不需要工具则输出最终 Markdown（## 关键发现/## 证据来源/## 下一步建议）。"
                    if strict_mode
                    else "不需要工具则直接输出回答正文（不必写最终 Markdown 结构）。"
                )
                if self._contains_dsml_markup(combined) and missing_tool_retry < max_missing_tool_retry:
                    missing_tool_retry += 1
                    print_info("[提示] 检测到 DSML 文本泄漏，正在请求模型改用标准 tool_calls 重试...")
                    self.messages.append(as_msg(_to_history_msg(assistant_msg)))
                    self._trim_messages()
                    self.messages.append(as_msg({
                        "role": "user",
                        "content": (
                            "禁止输出任何 DSML/XML/协议文本。请立即重试：\n"
                            "- 需要执行操作就必须发出标准 tool_calls；\n"
                            f"- {no_tool_instruction}\n"
                            "注意：不要把 tool_calls 以文本形式打印出来。"
                        ),
                    }))
                    self._trim_messages()
                    continue
                if finish_reason in {"length"} and missing_tool_retry < max_missing_tool_retry:
                    missing_tool_retry += 1
                    print_info("[提示] 流式输出因长度截断（finish_reason=length），正在请求继续...")
                    self.messages.append(as_msg(_to_history_msg(assistant_msg)))
                    self._trim_messages()
                    self.messages.append(as_msg({
                        "role": "user",
                        "content": (
                            "你的输出被截断了。请继续：如果需要操作请发 tool_calls；否则把回答补全。"
                        ),
                    }))
                    self._trim_messages()
                    continue
                if (not finish_reason) and combined and missing_tool_retry < max_missing_tool_retry:
                    missing_tool_retry += 1
                    print_info("[提示] 未检测到流式结束标志（finish_reason 为空），疑似未完整结束，正在请求继续...")
                    self.messages.append(as_msg(_to_history_msg(assistant_msg)))
                    self._trim_messages()
                    self.messages.append(as_msg({
                        "role": "user",
                        "content": (
                            "请继续完成上一轮输出：需要操作就发 tool_calls；不需要工具就直接给出最终回答正文。"
                        ),
                    }))
                    self._trim_messages()
                    continue
                if (not answer_started) and combined and missing_tool_retry < max_missing_tool_retry:
                    missing_tool_retry += 1
                    print_info("[提示] 本轮未开始输出[回答]，判定未完成，正在请求继续...")
                    self.messages.append(as_msg(_to_history_msg(assistant_msg)))
                    self._trim_messages()
                    self.messages.append(as_msg({
                        "role": "user",
                        "content": (
                            "你还没有开始输出[回答]，说明步骤未完成。\n"
                            "请继续：如果需要执行操作/取证，必须用 tool_calls 调用工具；"
                            "如果不需要工具了，请直接输出回答正文。"
                        ),
                    }))
                    self._trim_messages()
                    continue
                if strict_mode and combined and self._looks_like_missing_tool_call(
                        combined) and missing_tool_retry < max_missing_tool_retry:
                    missing_tool_retry += 1
                    print_info("[提示] 模型未生成 tool_calls，正在请求其以工具调用方式重试...")
                    self.messages.append(as_msg(_to_history_msg(assistant_msg)))
                    self._trim_messages()
                    self.messages.append(
                        as_msg(
                            {
                                "role": "user",
                                "content": (
                                    "请把你刚才的计划转换为真实的工具调用："
                                    "必须使用 tool_calls 调用需要的工具（例如 termux_command / r2_run_command 等），"
                                    "不要只输出文字描述。"
                                    "\n\n额外约束：只能使用 tools/list/schema 中存在的工具名；如果你写了不存在的工具名，请改用可用工具。"
                                    "如果你需要执行任意 shell 命令，请用 termux_command。"
                                ),
                            }
                        )
                    )
                    self._trim_messages()
                    continue
                if (not str(content).strip()) and str(
                        reasoning_text).strip() and missing_tool_retry < max_missing_tool_retry:
                    missing_tool_retry += 1
                    print_info("[提示] 模型仅输出思考内容，正在请求其继续完成（工具调用或最终结论）...")
                    self.messages.append(as_msg(_to_history_msg(assistant_msg)))
                    self._trim_messages()
                    self.messages.append(
                        as_msg(
                            {
                                "role": "user",
                                "content": (
                                    "你刚才只输出了思考过程，还没有完成结果。\n"
                                    "请继续：如果需要执行下一步操作，必须用 tool_calls 调用工具；"
                                    "如果不需要工具了，请直接输出回答正文。"
                                ),
                            }
                        )
                    )
                    self._trim_messages()
                    continue
                if re.search(r"(^|\n)\s*r2>\s*$", combined) and missing_tool_retry < max_missing_tool_retry:
                    missing_tool_retry += 1
                    print_info("[提示] 检测到模型输出包含提示符 r2>，疑似未完成，正在请求其继续...")
                    self.messages.append(as_msg(_to_history_msg(assistant_msg)))
                    self._trim_messages()
                    self.messages.append(
                        as_msg(
                            {
                                "role": "user",
                                "content": (
                                    "你刚才的输出以 `r2>` 结尾，说明步骤还没完成。\n"
                                    "请继续并立刻发出下一步需要的 tool_calls（不要只描述）。"
                                    "如果已经结束且不需要工具，请直接输出回答正文。"
                                ),
                            }
                        )
                    )
                    self._trim_messages()
                    continue
                final_text = str(content).strip()
                if strict_mode and (
                        not _looks_like_final_markdown(final_text)) and missing_tool_retry < max_missing_tool_retry:
                    missing_tool_retry += 1
                    print_info("[提示] 未检测到最终 Markdown 结论，继续请求模型完成...")
                    self.messages.append(as_msg(_to_history_msg(assistant_msg)))
                    self._trim_messages()
                    self.messages.append(
                        as_msg(
                            {
                                "role": "user",
                                "content": (
                                    "你尚未给出最终结论。\n"
                                    "请继续：要么立刻用 tool_calls 执行下一步（例如打开/分析已解压的 classes.dex、分析 .so 等），"
                                    "要么直接输出最终 Markdown（必须包含：## 关键发现 / ## 证据来源 / ## 下一步建议）。"
                                ),
                            }
                        )
                    )
                    self._trim_messages()
                    continue
                final_text = final_text or "(模型未返回文本)"
                final_text = _append_key_commands(final_text, trace_id)
                streamed_md = bool(assistant_msg.get("_streamed_markdown") is True)
                if RICH_AVAILABLE and final_text and _looks_like_markdown(final_text):
                    if not streamed_md:
                        if strict_mode:
                            print_info("[最终结果 Markdown 渲染]")
                        print_markdown(final_text)
                else:
                    print_info(final_text)
                self.messages.append(as_msg(_to_history_msg(assistant_msg)))
                self._trim_messages()
                return final_text
            missing_tool_retry = 0
            self.messages.append(as_msg(_to_history_msg(assistant_msg)))
            self._trim_messages()
            validation_errors: List[str] = []
            success_calls = 0
            recoverable_errors: List[str] = []
            success_tool_names: List[str] = []
            for tc in tool_calls:
                tool_name = tc["function"]["name"]
                args = self._parse_tool_arguments(tc)
                err = validate_args(tool_name, args, self.tool_specs)
                if err:
                    validation_errors.append(f"{tool_name}: {err}")
                    result = {"error": f"参数校验失败: {err}", "tool_name": tool_name, "arguments": args}
                    self.messages.append(as_msg(
                        {"role": "tool", "tool_call_id": tc["id"], "content": json.dumps(result, ensure_ascii=False)}))
                    continue
                print_info(f"[工具调用] {tool_name}")
                if debug_enabled():
                    debug_log(
                        "tool_call",
                        {
                            "trace_id": trace_id,
                            "turn_id": turn_id,
                            "tool_name": tool_name,
                            "args_keys": sorted([str(k) for k in args.keys()])[:40] if isinstance(args, dict) else [],
                        },
                    )
                result: Any = None
                declined = False
                if tool_name == "termux_command" and self.dangerous_policy != "off":
                    cmd = ""
                    if isinstance(args, dict):
                        cmd = str(args.get("command") or args.get("cmd") or args.get("shell") or "")
                    is_danger, reason = self._dangerous_action_for_termux_command(cmd)
                    if is_danger:
                        print_info(f"[提示] 检测到危险命令（{reason}）: {cmd}")
                        yn = "n"
                        if self.dangerous_policy == "confirm":
                            yn = input("是否继续执行该命令？(y/N): ").strip().lower()
                        if (self.dangerous_policy == "deny") or (yn != "y"):
                            declined = True
                            result = {
                                "error": f"危险命令已阻止({reason})",
                                "recoverable": True,
                                "tool_name": tool_name,
                                "arguments": args,
                                "command": cmd,
                            }
                try:
                    if declined:
                        if result is None:
                            result = {
                                "error": "危险命令已阻止",
                                "recoverable": True,
                                "tool_name": tool_name,
                                "arguments": args,
                            }
                    elif tool_name == "termux_save_script":
                        result = termux_save_script_wrapper(
                            bridge,
                            str(args.get("filename", "")),
                            str(args.get("content", "")),
                        )
                    else:
                        result = bridge.call_tool(tool_name, args)
                except KeyboardInterrupt as exc:
                    raise UserInterruptError("用户中断了当前工具执行") from exc
                except (requests.RequestException, JsonRpcError, ValueError, OSError) as exc:
                    result = {"error": str(exc), "tool_name": tool_name, "arguments": args}
                except Exception as exc:
                    result = {
                        "error": f"unexpected {type(exc).__name__}: {str(exc)[:200]}",
                        "unexpected": True,
                        "tool_name": tool_name,
                        "arguments": args,
                    }
                if isinstance(result, dict):
                    err_norm = extract_mcp_error_text(result)
                    if err_norm:
                        raw = result
                        result = {
                            "error": err_norm,
                            "recoverable": bool(isinstance(raw, dict) and raw.get("recoverable") is True),
                            "tool_name": tool_name,
                            "arguments": args,
                            "raw": self._compact_tool_result(tool_name, raw),
                        }
                result = self._compact_tool_result(tool_name, result)
                if isinstance(result, dict) and result.get("error"):
                    if result.get("recoverable"):
                        recoverable_errors.append(f"{tool_name}: {str(result.get('error'))[:160]}")
                    else:
                        errors.append(f"{tool_name}: {str(result.get('error'))[:120]}")
                        if len(errors) >= 4:
                            text = "工具调用连续失败，已停止自动循环。\n最近错误：\n" + "\n".join(
                                f"- {e}" for e in errors[-3:])
                            guidance = self._nonrecoverable_guidance(errors[-6:])
                            if guidance:
                                text += "\n\n" + guidance
                            print_info(text)
                            if debug_enabled():
                                debug_log("stop_after_failures", {"errors": errors[-6:]})
                            return text
                else:
                    errors = []
                    success_calls += 1
                    if tool_name not in success_tool_names:
                        success_tool_names.append(tool_name)
                if debug_enabled() and isinstance(result, dict):
                    debug_log(
                        "tool_result",
                        {
                            "trace_id": trace_id,
                            "turn_id": turn_id,
                            "tool_name": tool_name,
                            "ok": not bool(result.get("error")),
                            "recoverable": bool(result.get("recoverable") is True),
                            "error": str(result.get("error", "") or "")[:220],
                        },
                    )
                self.session_ids.update(extract_session_ids(result))
                tool_content = json.dumps(result, ensure_ascii=False)
                if len(tool_content) > self.max_tool_result_chars:
                    keep_head = int(self.max_tool_result_chars * 0.65)
                    keep_tail = self.max_tool_result_chars - keep_head
                    head = tool_content[:keep_head]
                    tail = tool_content[-keep_tail:] if keep_tail > 0 else ""
                    tool_content = (
                            head
                            + f"\\n...(工具结果过长已截断：总长 {len(tool_content)} 字符；保留头 {keep_head} + 尾 {keep_tail})...\\n"
                            + tail
                    )
                self.messages.append(as_msg({"role": "tool", "tool_call_id": tc["id"], "content": tool_content}))
                self._trim_messages()

            if (
                    recoverable_errors
                    and success_calls > 0
                    and recoverable_hint_retry < max_recoverable_hint_retry
                    and (not validation_errors)
            ):
                recoverable_hint_retry += 1
                print_info("[提示] 存在可恢复失败，已要求模型仅重试失败工具...")
                self.messages.append(as_msg({
                    "role": "user",
                    "content": self._build_recoverable_prompt(
                        success_tool_names=success_tool_names,
                        recoverable_errors=recoverable_errors,
                        partial_success=True,
                    ),
                }))
                self._trim_messages()
                continue

            if (
                    recoverable_errors
                    and success_calls == 0
                    and recoverable_retry < max_recoverable_retry
                    and (not validation_errors)
            ):
                recoverable_retry += 1
                print_info("[提示] 工具返回可恢复错误，正在请求模型自动修复并重发 tool_calls...")
                self.messages.append(as_msg({
                    "role": "user",
                    "content": self._build_recoverable_prompt(
                        success_tool_names=[],
                        recoverable_errors=recoverable_errors,
                        partial_success=False,
                    ),
                }))
                self._trim_messages()
                continue

            if validation_errors and success_calls == 0 and bad_validation_retry < max_bad_validation_retry:
                bad_validation_retry += 1
                print_info("[提示] 工具参数未通过 schema 校验，正在请求模型修正 tool_calls...")
                self.messages.append(as_msg({
                    "role": "user",
                    "content": (
                            "你刚才发出的 tool_calls 有参数校验失败（必填缺失/类型错误/包含未定义字段）。请修正后重发 tool_calls。\n"
                            "要求：\n"
                            "- 严格按 tools/list 的 inputSchema 填参；required 必须齐全且 string 必须非空；禁止额外字段。\n"
                            "- 直接输出标准 tool_calls，不要输出 DSML 或口头计划。\n"
                            "本轮校验错误摘要：\n"
                            + "\n".join(f"- {e}" for e in validation_errors[:6])
                    ),
                }))
                self._trim_messages()
                continue
            if success_calls > 0:
                bad_validation_retry = 0
                if not recoverable_errors:
                    recoverable_retry = 0
                    recoverable_hint_retry = 0
        summary_msg = self._stream_assistant_turn(tool_choice="none")
        content = str(summary_msg.get("content") or "").strip()
        if not content:
            content = (last_nonempty_answer or "本轮分析结束。").strip()
        if content:
            if RICH_AVAILABLE:
                print_info("[最终结果 Markdown 渲染]")
                print_markdown(content)
            else:
                print_info(content)
        self.messages.append(as_msg(summary_msg))
        self._trim_messages()
        return content

    def close_all_sessions(self, bridge: R2BridgeClient) -> None:
        for sid in sorted(self.session_ids):
            try:
                bridge.call_tool("r2_close_session", {"session_id": sid})
            except (requests.RequestException, OSError, ValueError):
                continue
