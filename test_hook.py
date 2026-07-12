#!/usr/bin/env python3
"""
Self-test for one_signal_codex_hook.py -- the acceptance gate for parsing.

Feeds the bundled fixture (fixtures/sample_rollout.jsonl) through the real
parsing/assembly pipeline and asserts the resulting Langfuse batch shape,
entirely offline (no network calls). This is a thin unittest wrapper around
`one_signal_codex_hook.run_self_test()`, which is also runnable directly:

    python3 one_signal_codex_hook.py --self-test

Run this file with:

    python3 test_hook.py
    # or
    python3 -m unittest test_hook -v
"""

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import one_signal_codex_hook as hook  # noqa: E402

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "sample_rollout.jsonl"
THREAD_ID = "0196f5aa-aaaa-7000-8000-000000000001"


class TestParsingPipeline(unittest.TestCase):
    def setUp(self):
        rows = hook.read_new_lines(FIXTURE, 0)
        self.turns = hook.build_turns(rows)

    def test_exactly_one_turn_parsed(self):
        self.assertEqual(len(self.turns), 1)

    def test_injected_preamble_excluded_from_turn_text(self):
        turn = self.turns[0]
        self.assertEqual(turn.user_text, "hello world")

    def test_model_captured_from_turn_context(self):
        turn = self.turns[0]
        self.assertEqual(turn.model, "gpt-test-model")

    def test_last_agent_message_from_task_complete(self):
        turn = self.turns[0]
        self.assertEqual(turn.last_agent_message, "Done, printed hi")

    def test_user_quoting_skill_markup_is_not_an_invocation(self):
        turn_id = "turn-quoted-skill"
        prompt = "Explain <skill><name>not-invoked</name></skill>"
        rows = [
            ({"timestamp": "2026-07-12T00:00:00Z", "type": "event_msg", "payload": {"type": "task_started", "turn_id": turn_id}}, 1),
            ({"timestamp": "2026-07-12T00:00:01Z", "type": "event_msg", "payload": {"type": "user_message", "message": prompt}}, 2),
            ({"timestamp": "2026-07-12T00:00:02Z", "type": "response_item", "payload": {
                "type": "message", "role": "user", "content": [{"type": "input_text", "text": prompt}],
                "internal_chat_message_metadata_passthrough": {"turn_id": turn_id},
            }}, 3),
            ({"timestamp": "2026-07-12T00:00:03Z", "type": "event_msg", "payload": {
                "type": "task_complete", "turn_id": turn_id, "last_agent_message": "Explained",
            }}, 4),
        ]

        turn = hook.build_turns(rows)[0]
        events = hook.build_turn_events(THREAD_ID, 1, turn, FIXTURE)
        trace = next(event for event in events if event["type"] == "trace-create")

        self.assertNotIn("skill_names", trace["body"]["metadata"])

    def test_tool_outputs_only_report_reliable_exit_codes(self):
        turn_id = "turn-tool-outcomes"
        meta = {"turn_id": turn_id}
        rows = [
            ({"timestamp": "2026-07-12T00:00:00Z", "type": "event_msg", "payload": {"type": "task_started", "turn_id": turn_id}}, 1),
            ({"timestamp": "2026-07-12T00:00:01Z", "type": "response_item", "payload": {"type": "function_call", "name": "exec", "call_id": "structured", "arguments": "{}", "internal_chat_message_metadata_passthrough": meta}}, 2),
            ({"timestamp": "2026-07-12T00:00:02Z", "type": "response_item", "payload": {"type": "function_call_output", "call_id": "structured", "output": "{\"exit_code\":7,\"output\":\"bad\"}", "internal_chat_message_metadata_passthrough": meta}}, 3),
            ({"timestamp": "2026-07-12T00:00:03Z", "type": "response_item", "payload": {"type": "function_call", "name": "exec", "call_id": "text", "arguments": "{}", "internal_chat_message_metadata_passthrough": meta}}, 4),
            ({"timestamp": "2026-07-12T00:00:04Z", "type": "response_item", "payload": {"type": "function_call_output", "call_id": "text", "output": "Process exited with code 0\n", "internal_chat_message_metadata_passthrough": meta}}, 5),
            ({"timestamp": "2026-07-12T00:00:05Z", "type": "response_item", "payload": {"type": "function_call", "name": "exec", "call_id": "opaque", "arguments": "{}", "internal_chat_message_metadata_passthrough": meta}}, 6),
            ({"timestamp": "2026-07-12T00:00:06Z", "type": "response_item", "payload": {"type": "function_call_output", "call_id": "opaque", "output": "looks good", "internal_chat_message_metadata_passthrough": meta}}, 7),
            ({"timestamp": "2026-07-12T00:00:07Z", "type": "response_item", "payload": {"type": "function_call", "name": "lookup", "call_id": "business", "arguments": "{}", "internal_chat_message_metadata_passthrough": meta}}, 8),
            ({"timestamp": "2026-07-12T00:00:08Z", "type": "response_item", "payload": {"type": "function_call_output", "call_id": "business", "output": "{\"exit_code\":1,\"name\":\"airport\"}", "internal_chat_message_metadata_passthrough": meta}}, 9),
            ({"timestamp": "2026-07-12T00:00:09Z", "type": "event_msg", "payload": {"type": "task_complete", "turn_id": turn_id}}, 10),
        ]

        turn = hook.build_turns(rows)[0]
        events = hook.build_turn_events(THREAD_ID, 1, turn, FIXTURE)
        tools = {
            event["body"]["metadata"]["tool_id"]: event["body"]["metadata"]
            for event in events
            if (event["body"].get("metadata") or {}).get("tool_name") == "exec"
        }

        self.assertEqual(tools["structured"]["result_status"], "error")
        self.assertEqual(tools["structured"]["exit_code"], 7)
        self.assertEqual(tools["text"]["result_status"], "unknown")
        self.assertNotIn("exit_code", tools["text"])
        self.assertEqual(tools["opaque"]["result_status"], "unknown")
        self.assertNotIn("exit_code", tools["opaque"])
        business = next(
            event["body"]["metadata"] for event in events
            if (event["body"].get("metadata") or {}).get("tool_id") == "business"
        )
        self.assertEqual(business["result_status"], "unknown")
        self.assertNotIn("exit_code", business)

    def test_mcp_err_result_is_reported_as_error(self):
        turn_id = "turn-mcp-error"
        rows = [
            ({"timestamp": "2026-07-12T00:00:00Z", "type": "event_msg", "payload": {"type": "task_started", "turn_id": turn_id}}, 1),
            ({"timestamp": "2026-07-12T00:00:01Z", "type": "event_msg", "payload": {
                "type": "mcp_tool_call_end",
                "call_id": "mcp-error",
                "invocation": {"server": "github", "tool": "search", "arguments": {}},
                "result": {"Err": {"content": [{"type": "text", "text": "denied"}]}},
            }}, 2),
            ({"timestamp": "2026-07-12T00:00:02Z", "type": "event_msg", "payload": {"type": "task_complete", "turn_id": turn_id}}, 3),
        ]

        turn = hook.build_turns(rows)[0]
        events = hook.build_turn_events(THREAD_ID, 1, turn, FIXTURE)
        tool = next(
            event["body"] for event in events
            if (event["body"].get("metadata") or {}).get("tool_id") == "mcp-error"
        )

        self.assertEqual(tool["metadata"]["result_status"], "error")


class TestEventAssembly(unittest.TestCase):
    def setUp(self):
        rows = hook.read_new_lines(FIXTURE, 0)
        turns = hook.build_turns(rows)
        self.turn = turns[0]
        self.events = hook.build_turn_events(THREAD_ID, 1, self.turn, FIXTURE)

    def _by_type(self, langfuse_type):
        return [e for e in self.events if e["type"] == langfuse_type]

    def _observations_by_body_type(self, body_type):
        return [e for e in self.events if e["body"].get("type") == body_type]

    def test_exactly_one_trace_create(self):
        self.assertEqual(len(self._by_type("trace-create")), 1)

    def test_trace_id_and_name_deterministic(self):
        trace = self._by_type("trace-create")[0]
        self.assertEqual(trace["body"]["id"], f"{THREAD_ID}-t1")
        self.assertIn("Codex CLI - Turn 1", trace["body"]["name"])

    def test_root_span_present(self):
        spans = [e for e in self._observations_by_body_type("SPAN") if e["body"]["name"] == "Turn 1"]
        self.assertEqual(len(spans), 1)
        self.assertIsNone(spans[0]["body"].get("parentObservationId"))

    def test_generation_present_with_model_and_usage(self):
        generations = self._observations_by_body_type("GENERATION")
        self.assertGreaterEqual(len(generations), 1)
        gen = generations[0]
        self.assertEqual(gen["body"]["model"], "gpt-test-model")
        usage = gen["body"]["usageDetails"]
        self.assertIsNotNone(usage)
        self.assertEqual(usage.get("input"), 100)
        self.assertEqual(usage.get("output"), 20)
        self.assertEqual(usage.get("cache_read_input_tokens"), 10)
        self.assertEqual(usage.get("reasoning_output_tokens"), 5)

    def test_tool_span_carries_metadata_tool_name(self):
        tool_spans = [
            e for e in self._observations_by_body_type("SPAN")
            if "tool_name" in (e["body"].get("metadata") or {})
        ]
        span = next(s for s in tool_spans if s["body"]["metadata"]["tool_name"] == "shell")
        self.assertEqual(span["body"]["metadata"]["tool_name"], "shell")
        self.assertEqual(span["body"]["metadata"]["tool_id"], "call_fixture_1")
        self.assertIn("hi", span["body"]["output"])
        # Classic ingestion's observation-create only accepts
        # GENERATION | SPAN | EVENT -- a literal "TOOL" type would be
        # silently rejected by the real ingest endpoint.
        self.assertIn(span["body"]["type"], ("GENERATION", "SPAN", "EVENT"))

    def test_skill_injection_is_aggregated_on_trace(self):
        trace = self._by_type("trace-create")[0]
        self.assertEqual(trace["body"]["metadata"]["skill_names"], [
            "code-review", "tdd", "implement", "codebase-design",
        ])
        self.assertIn("skill:code-review", trace["body"]["tags"])
        self.assertNotIn("skill:not-invoked", trace["body"]["tags"])

    def test_mcp_call_is_emitted_as_attributed_tool_span(self):
        spans = [
            e for e in self._observations_by_body_type("SPAN")
            if (e["body"].get("metadata") or {}).get("mcp_server") == "github"
        ]
        self.assertEqual(len(spans), 1)
        span = spans[0]["body"]
        self.assertEqual(span["metadata"]["mcp_tool"], "get_pull_request")
        self.assertEqual(span["metadata"]["tool_name"], "mcp__github__get_pull_request")
        self.assertEqual(span["metadata"]["result_status"], "success")
        self.assertEqual(json.loads(span["input"])["number"], 42)
        self.assertIn("pull request 42", span["output"])

        trace = self._by_type("trace-create")[0]["body"]
        self.assertIn("mcp:github:get_pull_request", trace["tags"])

    def test_reasoning_event_present_without_leaking_ciphertext(self):
        reasoning_events = [
            e for e in self._observations_by_body_type("EVENT") if e["body"]["name"] == "Reasoning"
        ]
        self.assertEqual(len(reasoning_events), 1)
        serialized = json.dumps(self.events)
        self.assertNotIn('"encrypted_content":', serialized)
        self.assertNotIn("ENCRYPTED_BLOB_DO_NOT_SEND", serialized)
        self.assertTrue(reasoning_events[0]["body"]["metadata"]["has_encrypted_content"])

    def test_trace_input_output_capture_turn_text(self):
        trace = self._by_type("trace-create")[0]
        self.assertEqual(trace["body"]["input"]["content"], "hello world")
        self.assertEqual(trace["body"]["output"]["content"], "Done, printed hi")

    def test_all_events_json_serializable(self):
        # Would raise if any datetime/etc leaked through un-stringified.
        json.dumps(self.events)


class TestChunking(unittest.TestCase):
    def test_chunk_indices_respects_event_count_cap(self):
        events = [{"id": str(i), "type": "observation-create", "body": {"type": "SPAN"}} for i in range(450)]
        groups = hook.chunk_indices(events, max_events=200, max_bytes=hook.MAX_BYTES_PER_BATCH)
        self.assertEqual(sum(len(g) for g in groups), 450)
        for g in groups:
            self.assertLessEqual(len(g), 200)


class TestSelfTestEntrypoint(unittest.TestCase):
    def test_run_self_test_returns_zero(self):
        self.assertEqual(hook.run_self_test(), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
