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
