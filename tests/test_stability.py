import json
import os
import sys
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


class StabilityTests(unittest.TestCase):
    class _DummyClient:
        pass

    class _DummyBridge:
        pass

    def test_compact_text_output_keeps_head_tail(self) -> None:
        from lib.analyzer import AIAnalyzer

        lines = [f"line{i}" for i in range(300)]
        text = "\n".join(lines)
        out = AIAnalyzer._compact_text_output(text, head_lines=5, tail_lines=7, max_chars=10_000)
        self.assertIn("line0", out)
        self.assertIn("line4", out)
        self.assertIn("line293", out)
        self.assertIn("...(中间省略", out)

    def test_compact_tool_result_termux_command(self) -> None:
        from lib.analyzer import AIAnalyzer

        a = AIAnalyzer(
            api_key="x",
            model="m",
            base_url="http://example.invalid",
            tool_specs={},
            client_override=self._DummyClient(),
        )
        long_stdout = "\n".join([f"out{i}" for i in range(400)])
        result = {"stdout": long_stdout, "stderr": "", "exit_code": 0}
        out = a._compact_tool_result("termux_command", result)
        self.assertIsInstance(out, dict)
        self.assertIn("stdout", out)
        self.assertIn("...(中间省略", out["stdout"])
        self.assertIn("out399", out["stdout"])

    def test_trim_messages_does_not_start_with_tool(self) -> None:
        from lib.analyzer import AIAnalyzer

        a = AIAnalyzer(
            api_key="x",
            model="m",
            base_url="http://example.invalid",
            tool_specs={},
            client_override=self._DummyClient(),
            max_context_chars=1200,
            max_context_messages=50,
        )
        a.messages = [
            {"role": "system", "content": "sys"},
            {"role": "assistant", "content": "a0",
             "tool_calls": [{"id": "tc1", "type": "function", "function": {"name": "t", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "tc1", "content": "X" * 3000},
            {"role": "tool", "tool_call_id": "tc_orphan", "content": "Y" * 3000},
            {"role": "user", "content": "u" * 2000},
        ]
        a._trim_messages()
        self.assertGreaterEqual(len(a.messages), 1)
        self.assertEqual(a.messages[0].get("role"), "system")
        if len(a.messages) >= 2:
            self.assertNotEqual(a.messages[1].get("role"), "tool")

    def test_trim_messages_trims_by_chars_even_if_count_ok(self) -> None:
        from lib.analyzer import AIAnalyzer

        a = AIAnalyzer(
            api_key="x",
            model="m",
            base_url="http://example.invalid",
            tool_specs={},
            client_override=self._DummyClient(),
            max_context_chars=1000,
            max_context_messages=999,  # count won't trigger
        )
        a.messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "U" * 800},
            {"role": "assistant", "content": "A" * 800},
            {"role": "user", "content": "TAIL"},
        ]
        before = len(a.messages)
        a._trim_messages()
        after = len(a.messages)
        self.assertLessEqual(after, before)
        self.assertEqual(a.messages[0].get("role"), "system")

    def test_trim_messages_preserves_tool_pairing(self) -> None:
        from lib.analyzer import AIAnalyzer

        a = AIAnalyzer(
            api_key="x",
            model="m",
            base_url="http://example.invalid",
            tool_specs={},
            client_override=self._DummyClient(),
            max_context_chars=1400,
            max_context_messages=999,
        )
        a.messages = [
            {"role": "system", "content": "sys"},
            {"role": "assistant", "content": "a0",
             "tool_calls": [{"id": "tc1", "type": "function", "function": {"name": "t", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "tc1", "content": "X" * 1200},
            {"role": "assistant", "content": "a1",
             "tool_calls": [{"id": "tc2", "type": "function", "function": {"name": "t", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "tc2", "content": "Y" * 1200},
            {"role": "user", "content": "tail"},
        ]
        a._trim_messages()

        allowed: set[str] = set()
        for msg in a.messages:
            if msg.get("role") == "assistant":
                allowed = set()
                tcs = msg.get("tool_calls") or []
                if isinstance(tcs, list):
                    for tc in tcs:
                        if isinstance(tc, dict) and isinstance(tc.get("id"), str) and tc.get("id"):
                            allowed.add(tc["id"])
            elif msg.get("role") == "tool":
                self.assertIn(msg.get("tool_call_id"), allowed)

    def test_sanitize_drops_unresponded_tool_calls(self) -> None:
        from lib.analyzer import AIAnalyzer

        a = AIAnalyzer(
            api_key="x",
            model="m",
            base_url="http://example.invalid",
            tool_specs={},
            client_override=self._DummyClient(),
            max_context_chars=999999,
            max_context_messages=999,
        )
        a.messages = [
            {"role": "system", "content": "sys"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "tc1", "type": "function", "function": {"name": "t", "arguments": "{}"}},
                {"id": "tc2", "type": "function", "function": {"name": "t", "arguments": "{}"}},
            ]},
            {"role": "tool", "tool_call_id": "tc1", "content": "ok"},
            # tc2 missing tool response
            {"role": "user", "content": "next"},
        ]
        a._trim_messages()
        # assistant.tool_calls should not contain tc2 after sanitization
        am = a.messages[1]
        self.assertEqual(am.get("role"), "assistant")
        tcs = am.get("tool_calls") or []
        ids = [tc.get("id") for tc in tcs if isinstance(tc, dict)]
        self.assertIn("tc1", ids)
        self.assertNotIn("tc2", ids)

    def test_recoverable_guidance_mentions_last_r2_file_path(self) -> None:
        from lib.analyzer import AIAnalyzer

        a = AIAnalyzer(
            api_key="x",
            model="m",
            base_url="http://example.invalid",
            tool_specs={},
            client_override=self._DummyClient(),
        )
        a.last_r2_file_path = "/storage/emulated/0/a.so"
        g = a._recoverable_guidance(["r2_disassemble: Invalid session_id: session_xxx"])
        self.assertIn("r2_open_file", g)
        self.assertIn("/storage/emulated/0/a.so", g)

    def test_rewrite_termux_sandbox_paths(self) -> None:
        from lib.analyzer import AIAnalyzer

        self.assertEqual(
            AIAnalyzer._rewrite_termux_sandbox_paths("mkdir -p /data/data/com.termux/tmp"),
            "mkdir -p /data/data/com.termux/files/home/AI/tmp",
        )
        self.assertEqual(
            AIAnalyzer._rewrite_termux_sandbox_paths("mkdir -p /data/data/com.termux/AI/tmp"),
            "mkdir -p /data/data/com.termux/files/home/AI/tmp",
        )
        # termux 内部可用路径不应被改写
        self.assertEqual(
            AIAnalyzer._rewrite_termux_sandbox_paths("cd /data/data/com.termux/files/home && ls"),
            "cd /data/data/com.termux/files/home && ls",
        )

    def test_dashscope_thinking_merges_extra_body(self) -> None:
        from lib.analyzer import AIAnalyzer

        a = AIAnalyzer(
            api_key="x",
            model="qwen-plus",
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            tool_specs={},
            client_override=self._DummyClient(),
            enable_search=True,
            enable_thinking=True,
            thinking_budget=50,
        )
        req: dict = {}
        a._maybe_enable_web_search(req)
        a._maybe_enable_dashscope_deep_thinking(req)
        eb = req.get("extra_body")
        self.assertIsInstance(eb, dict)
        self.assertEqual(eb.get("enable_search"), True)
        self.assertEqual(eb.get("enable_thinking"), True)
        self.assertEqual(eb.get("thinking_budget"), 50)

    def test_parse_json_tool_calls(self) -> None:
        from lib.analyzer import AIAnalyzer

        text = '''
思考：需要先打开文件
```
{"name": "r2_open_file", "arguments": {"file_path": "/path/to/a.so"}}
{"name": "termux_command", "arguments": {"command": "ls -la"}}
```
'''
        calls = AIAnalyzer._parse_json_tool_calls(text)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0]["function"]["name"], "r2_open_file")
        self.assertEqual(json.loads(calls[0]["function"]["arguments"]), {"file_path": "/path/to/a.so"})
        self.assertEqual(calls[1]["function"]["name"], "termux_command")

    def test_model_supports_enable_search_and_thinking(self) -> None:
        from lib.analyzer import AIAnalyzer

        self.assertTrue(AIAnalyzer._model_supports_enable_search("qwen-plus"))
        self.assertTrue(AIAnalyzer._model_supports_enable_search("deepseek-r1"))
        self.assertFalse(AIAnalyzer._model_supports_enable_search("deepseek-r1-distill-llama-8b"))
        self.assertFalse(AIAnalyzer._model_supports_enable_thinking("deepseek-r1-distill-llama-8b"))
        self.assertTrue(AIAnalyzer._model_supports_enable_thinking("qwen-plus"))
        self.assertTrue(AIAnalyzer._model_supports_enable_thinking("deepseek-v3.2"))

    def test_use_text_tool_mode_dashscope_distill(self) -> None:
        from lib.analyzer import AIAnalyzer

        a = AIAnalyzer(
            api_key="x",
            model="deepseek-r1-distill-llama-8b",
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            tool_specs={},
            client_override=self._DummyClient(),
        )
        self.assertTrue(a._use_text_tool_mode())
        b = AIAnalyzer(
            api_key="x",
            model="qwen-plus",
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            tool_specs={},
            client_override=self._DummyClient(),
        )
        self.assertFalse(b._use_text_tool_mode())

    def test_deepseek_distill_skips_thinking_when_tools(self) -> None:
        from lib.analyzer import AIAnalyzer

        a = AIAnalyzer(
            api_key="x",
            model="deepseek-r1-distill-llama-8b",
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            tool_specs={"t": {"properties": {}, "required": []}},
            client_override=self._DummyClient(),
            enable_thinking=True,
            thinking_budget=50,
        )
        req: dict = {}
        a._maybe_enable_dashscope_deep_thinking(req, tool_choice="auto")
        eb = req.get("extra_body")
        self.assertIsNone(eb)

    def test_trim_never_leaves_orphan_assistant_with_tool_calls(self) -> None:
        """裁剪后不得留下孤立的 assistant(tool_calls)，否则会触发 Invalid consecutive assistant message"""
        from lib.analyzer import AIAnalyzer

        a = AIAnalyzer(
            api_key="x",
            model="m",
            base_url="http://example.invalid",
            tool_specs={},
            client_override=self._DummyClient(),
            max_context_messages=5,
            max_context_chars=999999,
        )
        # 构造：裁剪后 tail 末尾可能是 assistant(tool_calls) 且无 tool 响应
        a.messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "u1"},
            {"role": "assistant", "content": "a1"},
            {"role": "user", "content": "u2"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "tc1", "type": "function", "function": {"name": "t", "arguments": "{}"}},
            ]},
            # 无 tool 响应，裁剪后可能只剩 assistant
        ]
        a._trim_messages()
        # 最终不得有 assistant(tool_calls) 且其后无 tool
        for i, m in enumerate(a.messages):
            if not isinstance(m, dict):
                continue
            if m.get("role") != "assistant":
                continue
            tcs = m.get("tool_calls")
            if not (isinstance(tcs, list) and tcs):
                continue
            # 有 tool_calls 的 assistant 必须有后续 tool
            next_idx = i + 1
            if next_idx >= len(a.messages):
                self.fail(f"assistant at {i} has tool_calls but no following tool message")
            next_msg = a.messages[next_idx]
            if not isinstance(next_msg, dict) or next_msg.get("role") != "tool":
                self.fail(f"assistant at {i} has tool_calls but next is {next_msg.get('role')}")

    def test_recoverable_prompt_mentions_success_tools(self) -> None:
        from lib.analyzer import AIAnalyzer

        a = AIAnalyzer(
            api_key="x",
            model="m",
            base_url="http://example.invalid",
            tool_specs={},
            client_override=self._DummyClient(),
        )
        text = a._build_recoverable_prompt(
            success_tool_names=["termux_command", "r2_open_file"],
            recoverable_errors=["r2_run_command: session invalid"],
            partial_success=True,
        )
        self.assertIn("本轮已成功工具", text)
        self.assertIn("termux_command", text)
        self.assertIn("不要重复", text)

    def test_nonrecoverable_guidance_rate_limit(self) -> None:
        from lib.analyzer import AIAnalyzer

        a = AIAnalyzer(
            api_key="x",
            model="m",
            base_url="http://example.invalid",
            tool_specs={},
            client_override=self._DummyClient(),
        )
        g = a._nonrecoverable_guidance(["termux_command: 429 Too Many Requests"])
        self.assertTrue(("限流" in g) or ("429" in g))

    def test_dangerous_termux_command_detection(self) -> None:
        from lib.analyzer import AIAnalyzer

        ok, reason = AIAnalyzer._is_dangerous_termux_command("rm -rf /")
        self.assertTrue(ok)
        self.assertIn("rm", reason)

    def test_termux_compact_extracts_error_lines(self) -> None:
        from lib.analyzer import AIAnalyzer

        a = AIAnalyzer(
            api_key="x",
            model="m",
            base_url="http://example.invalid",
            tool_specs={},
            client_override=self._DummyClient(),
        )
        stdout = "\n".join([f"noise{i}" for i in range(50)]) + "\nPermission denied: /data\n" + "\n".join(
            [f"tail{i}" for i in range(50)])
        out = a._compact_tool_result("termux_command", {"stdout": stdout})
        self.assertIn("Permission denied", str(out.get("stdout", "")))

    def test_chat_loose_mode_allows_plain_answer_without_retries(self) -> None:
        from lib.analyzer import AIAnalyzer

        class _FakeAnalyzer(AIAnalyzer):
            def __init__(self) -> None:
                super().__init__(
                    api_key="x",
                    model="m",
                    base_url="http://example.invalid",
                    tool_specs={},  # no tools needed for this test
                    client_override=StabilityTests._DummyClient(),
                )
                self.calls = 0

            def _stream_assistant_turn(self, tool_choice: str = "auto") -> dict:
                self.calls += 1
                return {
                    "role": "assistant",
                    "content": "OK",
                    "reasoning_content": "",
                    "_raw_content": "OK",
                    "_finish_reason": "stop",
                    "_answer_started": True,
                    "_thinking_started": False,
                }

        a = _FakeAnalyzer()
        bridge = StabilityTests._DummyBridge()

        out_loose = a.chat("不要分析，只回答：OK", bridge, mode="loose")
        self.assertEqual(out_loose.strip(), "OK")
        self.assertEqual(a.calls, 1)
        self.assertEqual(len(a.messages), 3)  # system + user + assistant

        b = _FakeAnalyzer()
        out_strict = b.chat("不要分析，只回答：OK", bridge, mode="strict")
        self.assertEqual(out_strict.strip(), "OK")
        self.assertGreater(b.calls, 1)


if __name__ == "__main__":
    unittest.main()
