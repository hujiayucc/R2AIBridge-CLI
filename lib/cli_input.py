from __future__ import annotations

import os
import re
import sys
from typing import Iterable, List, Tuple, Union

from lib.commands.context import CommandContext

HISTORY_PATH = os.path.join(".", ".r2_cli_history")
_PROMPT_TOOLKIT_DISABLED = False

_EXTRA_PROMPT_TOOLKIT_EXC: tuple[type[BaseException], ...] = ()
_pt_win32 = None
try:
    if sys.platform == "win32":
        import prompt_toolkit.output.win32 as _pt_win32

        _ncsbe = getattr(_pt_win32, "NoConsoleScreenBufferError", None)
        if isinstance(_ncsbe, type) and issubclass(_ncsbe, BaseException):
            _EXTRA_PROMPT_TOOLKIT_EXC = (_ncsbe,)
except (ImportError, AssertionError):
    _EXTRA_PROMPT_TOOLKIT_EXC = ()
    _pt_win32 = None

_PROMPT_TOOLKIT_FALLBACK_EXC: tuple[type[BaseException], ...] = (
                                                                    EOFError,
                                                                    OSError,
                                                                    RuntimeError,
                                                                    TypeError,
                                                                    ValueError,
                                                                    AttributeError,
                                                                ) + _EXTRA_PROMPT_TOOLKIT_EXC

CompletionCand = Union[str, Tuple[str, int]]


def _root_commands() -> List[str]:
    return [
        "help",
        "health",
        "self_check",
        "tools",
        "list",
        "call",
        "ai",
        "ai_reset",
        "ai_reload",
        "bridge_reload",
        "debug",
        "config",
        "status",
        "session",
        "apk_analyze",
        "dex_analyze",
        "so_analyze",
        "exit",
        "quit",
        "q",
    ]


def _session_subcommands() -> List[str]:
    return ["list", "use", "close", "help"]


def _debug_subcommands() -> List[str]:
    return ["on", "off", "path", "tail", "trace", "export", "max_bytes"]


def _config_subcommands() -> List[str]:
    return ["keys", "show", "set"]


def _ai_flags() -> List[str]:
    return ["--loose", "--plain", "--strict", "--tools"]


def _tool_names(ctx: CommandContext) -> List[str]:
    specs = getattr(ctx.schema_module, "ACTIVE_TOOL_SPECS", None)
    if not isinstance(specs, dict):
        return []
    out: List[str] = []
    for k in specs.keys():
        s = str(k or "").strip()
        if s:
            out.append(s)
    return sorted(set(out))


def _tool_spec(ctx: CommandContext, tool_name: str) -> dict:
    specs = getattr(ctx.schema_module, "ACTIVE_TOOL_SPECS", None)
    if not isinstance(specs, dict):
        return {}
    spec = specs.get(tool_name)
    return spec if isinstance(spec, dict) else {}


def _tool_arg_snippets(ctx: CommandContext, tool_name: str, already_keys: set[str]) -> List[str]:
    spec = _tool_spec(ctx, tool_name)
    props = spec.get("properties") if isinstance(spec, dict) else None
    required = spec.get("required") if isinstance(spec, dict) else None
    if not isinstance(props, dict):
        props = {}
    if not isinstance(required, list):
        required = []
    req_keys = [str(x) for x in required if isinstance(x, str) and x.strip()]

    def _placeholder(key: str) -> List[str]:
        ps = props.get(key) if isinstance(props, dict) else None
        typ = ps.get("type") if isinstance(ps, dict) else ""
        if key == "session_id":
            sid = str(getattr(ctx, "active_session_id", "") or "").strip()
            if sid:
                return [f"\"{key}\": \"{sid}\""]
            return [f"\"{key}\": \"\""]
        if typ == "string":
            return [f"\"{key}\": \"\""]
        if typ == "integer":
            return [f"\"{key}\": 0"]
        if typ == "boolean":
            return [f"\"{key}\": true", f"\"{key}\": false"]
        if typ == "object":
            return [f"\"{key}\": {{}}"]
        if typ == "array":
            return [f"\"{key}\": []"]
        return [f"\"{key}\": null"]

    out: List[str] = []
    for k in req_keys:
        if k in already_keys:
            continue
        out.extend(_placeholder(k))
    for k in sorted([str(x) for x in props.keys() if isinstance(x, str) and x.strip()]):
        if k in already_keys or k in req_keys:
            continue
        out.extend(_placeholder(k))
    return out


def _session_ids(ctx: CommandContext) -> List[str]:
    s = set(ctx.known_sessions)
    try:
        if ctx.analyzer is not None:
            s.update(set(getattr(ctx.analyzer, "session_ids", set()) or set()))
    except (TypeError, ValueError, AttributeError):
        pass
    return sorted([x for x in s if isinstance(x, str) and x])


def _config_keys(ctx: CommandContext) -> List[str]:
    keys = list(ctx.current_config.keys()) if isinstance(ctx.current_config, dict) else []
    return sorted({str(k) for k in keys if isinstance(k, str) and k.strip()})


def read_command(prompt_text: str, ctx: CommandContext) -> str:
    global _PROMPT_TOOLKIT_DISABLED
    if _PROMPT_TOOLKIT_DISABLED:
        return input(prompt_text).strip()

    try:
        if (not os.isatty(0)) or (not os.isatty(1)):
            return input(prompt_text).strip()
    except OSError:
        pass

    try:
        from prompt_toolkit import prompt
        from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
        from prompt_toolkit.completion import Completer, Completion
        from prompt_toolkit.history import FileHistory
    except ImportError:
        return input(prompt_text).strip()

    class _DynCompleter(Completer):
        def get_completions(self, document, complete_event) -> Iterable[Completion]:
            _ = self
            _ = complete_event
            text = document.text_before_cursor or ""
            stripped = text.lstrip()
            ends_with_space = bool(stripped) and stripped[-1].isspace()
            parts = stripped.split()
            if ends_with_space:
                parts.append("")
            word = document.get_word_before_cursor(WORD=True) or ""

            def _emit(cands: List[CompletionCand]) -> Iterable[Completion]:
                lw = word.lower()
                seen: set[str] = set()
                for item in cands:
                    if not item:
                        continue
                    if isinstance(item, tuple):
                        c, start_pos = item
                    else:
                        c, start_pos = item, -len(word)
                    if not c:
                        continue
                    key = str(c).lower()
                    if key in seen:
                        continue
                    seen.add(key)
                    if lw and (not key.startswith(lw)):
                        continue
                    yield Completion(str(c), start_position=int(start_pos))

            if not parts:
                yield from _emit(_root_commands())
                return

            cmd = parts[0].lower()
            if len(parts) == 1:
                yield from _emit(_root_commands())
                return

            if cmd == "call":
                if len(parts) == 2:
                    yield from _emit(_tool_names(ctx))
                    return
                tool_name = str(parts[1] or "").strip()
                if not tool_name:
                    return
                if "{" not in stripped:
                    yield from _emit(["{}", "{ }", "{\n  \n}"])
                    return
                frag = stripped[stripped.find("{"):]
                present = set(re.findall(r"\"([A-Za-z0-9_]+)\"\s*:", frag))
                snippets = _tool_arg_snippets(ctx, tool_name, present)
                json_multiline = ("\n" in frag) or (re.search(r"\{\s*\n", frag) is not None)

                tail_empty = re.search(r"\{\s*}\s*$", stripped)
                if tail_empty:
                    tail_len = len(tail_empty.group(0))
                    repls: List[CompletionCand] = []
                    for s in snippets[:40]:
                        if json_multiline:
                            repls.append((f"{{\n  {s}\n}}", -tail_len))
                        else:
                            repls.append((f"{{ {s} }}", -tail_len))
                    yield from _emit(repls)
                    return
                tail_close = re.search(r"}\s*$", stripped)
                if tail_close:
                    tail_len = len(tail_close.group(0))
                    repls = []
                    for s in snippets[:40]:
                        if json_multiline:
                            repls.append((f",\n  {s}\n}}", -tail_len))
                        else:
                            sep = "" if frag.rstrip().endswith("{") else ", "
                            repls.append((f"{sep}{s} }}", -tail_len))
                    yield from _emit(repls)
                    return

                yield from _emit(snippets)
                return

            if cmd == "ai":
                if len(parts) == 2 and parts[1].startswith("--"):
                    yield from _emit(_ai_flags())
                return

            if cmd == "session":
                if len(parts) == 2:
                    yield from _emit(_session_subcommands())
                    return
                if len(parts) == 3 and parts[1].lower() in {"use", "close"}:
                    yield from _emit(_session_ids(ctx))
                    return
                return

            if cmd == "debug":
                if len(parts) == 2:
                    yield from _emit(_debug_subcommands())
                    return
                if len(parts) == 3 and parts[1].lower() in {"trace", "export"}:
                    extra = []
                    if getattr(ctx, "last_ai_trace_id", ""):
                        extra.append(str(ctx.last_ai_trace_id))
                    extra.append("last")
                    yield from _emit(extra)
                    return
                return

            if cmd == "config":
                if len(parts) == 2:
                    yield from _emit(_config_subcommands())
                    return
                if len(parts) == 3 and parts[1].lower() == "set":
                    yield from _emit(_config_keys(ctx))
                    return
                return

    hist_dir = os.path.dirname(os.path.abspath(HISTORY_PATH))
    try:
        os.makedirs(hist_dir, exist_ok=True)
    except OSError:
        pass
    try:
        return prompt(
            prompt_text,
            completer=_DynCompleter(),
            complete_while_typing=True,
            history=FileHistory(HISTORY_PATH),
            auto_suggest=AutoSuggestFromHistory(),
        ).strip()
    except KeyboardInterrupt:
        raise
    except _PROMPT_TOOLKIT_FALLBACK_EXC as _exc:
        _ = _exc
        _PROMPT_TOOLKIT_DISABLED = True
        return input(prompt_text).strip()
