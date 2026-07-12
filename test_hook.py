#!/usr/bin/env python3
"""
Self-test for one_signal_codex_hook.py -- the acceptance gate for parsing.

Feeds the bundled fixture (fixtures/sample_rollout.jsonl) through the real
parsing/assembly pipeline and asserts the resulting Langfuse batch shape,
entirely offline (no network calls). This is a thin unittest wrapper around
`one_signal_codex_hook.run_self_test()`, which is also runnable directly:

    uv run python one_signal_codex_hook.py --self-test

Run this file with:

    uv run python test_hook.py
    # or
    uv run python -m unittest test_hook -v
"""

import json
import io
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

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
        # Uses the real rollout ordering: Codex emits the user-role
        # response_item copy of the prompt BEFORE the authoritative
        # event_msg/user_message. A prompt that merely quotes skill markup
        # mid-sentence must not be classified as a skill invocation.
        turn_id = "turn-quoted-skill"
        prompt = "Explain <skill><name>not-invoked</name></skill>"
        rows = [
            ({"timestamp": "2026-07-12T00:00:00Z", "type": "event_msg", "payload": {"type": "task_started", "turn_id": turn_id}}, 1),
            ({"timestamp": "2026-07-12T00:00:01Z", "type": "response_item", "payload": {
                "type": "message", "role": "user", "content": [{"type": "input_text", "text": prompt}],
                "internal_chat_message_metadata_passthrough": {"turn_id": turn_id},
            }}, 2),
            ({"timestamp": "2026-07-12T00:00:02Z", "type": "event_msg", "payload": {"type": "user_message", "message": prompt}}, 3),
            ({"timestamp": "2026-07-12T00:00:03Z", "type": "event_msg", "payload": {
                "type": "task_complete", "turn_id": turn_id, "last_agent_message": "Explained",
            }}, 4),
        ]

        turn = hook.build_turns(rows)[0]
        events = hook.build_turn_events(THREAD_ID, 1, turn, FIXTURE)
        trace = next(event for event in events if event["type"] == "trace-create")

        self.assertNotIn("skill_names", trace["body"]["metadata"])

    def test_user_prompt_starting_with_skill_markup_is_not_an_invocation(self):
        turn_id = "turn-prompt-starts-with-skill"
        prompt = "<skill><name>user-text</name></skill> please explain"
        rows = [
            ({"timestamp": "2026-07-12T00:00:00Z", "type": "event_msg", "payload": {"type": "task_started", "turn_id": turn_id}}, 1),
            ({"timestamp": "2026-07-12T00:00:01Z", "type": "response_item", "payload": {
                "type": "message", "role": "user", "content": [{"type": "input_text", "text": prompt}],
                "internal_chat_message_metadata_passthrough": {"turn_id": turn_id},
            }}, 2),
            ({"timestamp": "2026-07-12T00:00:02Z", "type": "event_msg", "payload": {"type": "user_message", "message": prompt}}, 3),
            ({"timestamp": "2026-07-12T00:00:03Z", "type": "event_msg", "payload": {
                "type": "task_complete", "turn_id": turn_id, "last_agent_message": "Explained",
            }}, 4),
        ]

        turn = hook.build_turns(rows)[0]
        events = hook.build_turn_events(THREAD_ID, 1, turn, FIXTURE)
        trace = next(event for event in events if event["type"] == "trace-create")

        self.assertNotIn("skill_names", trace["body"]["metadata"])

    def test_real_skill_injection_before_authoritative_prompt_is_detected(self):
        # In most real rollouts, the Skill injection arrives before the
        # authoritative event_msg/user_message.
        turn_id = "turn-real-skill"
        rows = [
            ({"timestamp": "2026-07-12T00:00:00Z", "type": "event_msg", "payload": {"type": "task_started", "turn_id": turn_id}}, 1),
            ({"timestamp": "2026-07-12T00:00:01Z", "type": "response_item", "payload": {
                "type": "message", "role": "user", "content": [{"type": "input_text", "text": "review my change"}],
                "internal_chat_message_metadata_passthrough": {"turn_id": turn_id},
            }}, 2),
            ({"timestamp": "2026-07-12T00:00:02Z", "type": "response_item", "payload": {
                "type": "message", "role": "user",
                "content": [{"type": "input_text", "text": "<skill>\n<name>code-review</name>\n</skill>\nfull skill body here"}],
                "internal_chat_message_metadata_passthrough": {"turn_id": turn_id},
            }}, 3),
            ({"timestamp": "2026-07-12T00:00:03Z", "type": "event_msg", "payload": {"type": "user_message", "message": "review my change"}}, 4),
            ({"timestamp": "2026-07-12T00:00:04Z", "type": "event_msg", "payload": {
                "type": "task_complete", "turn_id": turn_id, "last_agent_message": "Reviewed",
            }}, 5),
        ]

        turn = hook.build_turns(rows)[0]
        events = hook.build_turn_events(THREAD_ID, 1, turn, FIXTURE)
        trace = next(event for event in events if event["type"] == "trace-create")

        self.assertEqual(trace["body"]["metadata"]["skill_names"], ["code-review"])
        self.assertIn("skill:code-review", trace["body"]["tags"])

    def test_first_turn_uploads_global_and_project_instruction_documents(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            project = home / "work" / "project"
            nested = project / "packages" / "app"
            (project / ".git").mkdir(parents=True)
            nested.mkdir(parents=True)
            (home / ".codex").mkdir()
            (home / ".claude").mkdir()
            (home / ".codex" / "AGENTS.md").write_text("global agents", encoding="utf-8")
            (home / ".claude" / "CLAUDE.md").write_text("global claude", encoding="utf-8")
            (project / "AGENTS.md").write_text("project agents", encoding="utf-8")
            (nested / "CLAUDE.md").write_text("nested claude", encoding="utf-8")
            turn = self.turns[0]
            turn.cwd = str(nested)

            with mock.patch.object(hook, "CODEX_HOME", home / ".codex"), mock.patch.object(hook.Path, "home", return_value=home):
                events = hook.build_turn_events(THREAD_ID, 1, turn, FIXTURE)

            trace = next(event for event in events if event["type"] == "trace-create")
            documents = trace["body"]["metadata"]["instruction_documents"]
            self.assertEqual([document["path"] for document in documents], [
                "~/.codex/AGENTS.md",
                "~/.claude/CLAUDE.md",
                "AGENTS.md",
                "packages/app/CLAUDE.md",
            ])

            later = hook.build_turn_events(THREAD_ID, 2, turn, FIXTURE)
            later_trace = next(event for event in later if event["type"] == "trace-create")
            self.assertNotIn("instruction_documents", later_trace["body"]["metadata"])

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

    def test_aborted_turn_is_emitted_as_completed_warning(self):
        turn_id = "turn-aborted"
        rows = [
            ({"timestamp": "2026-07-12T00:00:00Z", "type": "event_msg", "payload": {"type": "task_started", "turn_id": turn_id}}, 1),
            ({"timestamp": "2026-07-12T00:00:01Z", "type": "event_msg", "payload": {"type": "user_message", "message": "stop this"}}, 2),
            ({"timestamp": "2026-07-12T00:00:02Z", "type": "event_msg", "payload": {"type": "turn_aborted", "turn_id": turn_id}}, 3),
        ]

        turn = hook.build_turns(rows)[0]
        trace = hook.build_turn_events(THREAD_ID, 1, turn, FIXTURE)[0]

        self.assertTrue(turn.complete)
        self.assertTrue(turn.aborted)
        self.assertEqual(trace["body"]["metadata"]["completed"], True)
        self.assertEqual(trace["body"]["metadata"]["aborted"], True)


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
        # The fixture yields exactly two tool spans: the shell call and the
        # MCP call. An exact count guards against duplicate or orphan spans.
        self.assertEqual(len(tool_spans), 2)
        span = next(s for s in tool_spans if s["body"]["metadata"]["tool_name"] == "shell")
        self.assertEqual(span["body"]["metadata"]["tool_id"], "call_fixture_1")
        self.assertIn("hi", span["body"]["output"])
        # Classic ingestion's observation-create only accepts
        # GENERATION | SPAN | EVENT -- a literal "TOOL" type would be
        # silently rejected by the real ingest endpoint.
        self.assertIn(span["body"]["type"], ("GENERATION", "SPAN", "EVENT"))

    def test_skill_injection_is_aggregated_on_trace(self):
        trace = self._by_type("trace-create")[0]
        self.assertEqual(trace["body"]["metadata"]["skill_names"], ["code-review"])
        self.assertIn("skill:code-review", trace["body"]["tags"])
        # The assistant-role message that merely quotes `<skill><name>...`
        # markup must never be counted as an invocation.
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

    def test_mcp_call_without_paired_function_call_is_recovered(self):
        # Fallback path: an mcp_tool_call_end with no preceding function_call
        # (dict arguments, an Err result) must still yield one attributed SPAN
        # whose input is JSON-parseable, whose output carries the error text,
        # and whose latency is recovered from the event's `duration` rather
        # than collapsed to zero.
        turn_id = "turn-orphan-mcp"
        rows = [
            ({"timestamp": "2026-07-12T00:00:00.000Z", "type": "event_msg", "payload": {"type": "task_started", "turn_id": turn_id}}, 1),
            ({"timestamp": "2026-07-12T00:00:00.100Z", "type": "event_msg", "payload": {"type": "user_message", "message": "search it"}}, 2),
            ({"timestamp": "2026-07-12T00:00:02.000Z", "type": "event_msg", "payload": {
                "type": "mcp_tool_call_end",
                "call_id": "call_orphan_1",
                "invocation": {"server": "exa", "tool": "web_search", "arguments": {"query": "hi"}},
                "duration": {"secs": 1, "nanos": 500000000},
                "result": {"Err": {"content": [{"type": "text", "text": "rate limited"}]}},
            }}, 3),
            ({"timestamp": "2026-07-12T00:00:02.100Z", "type": "event_msg", "payload": {
                "type": "task_complete", "turn_id": turn_id, "last_agent_message": "done",
            }}, 4),
        ]

        turn = hook.build_turns(rows)[0]
        events = hook.build_turn_events(THREAD_ID, 1, turn, FIXTURE)
        spans = [
            e for e in events
            if e["body"].get("type") == "SPAN" and (e["body"].get("metadata") or {}).get("mcp_server") == "exa"
        ]
        self.assertEqual(len(spans), 1)
        body = spans[0]["body"]
        self.assertEqual(body["metadata"]["mcp_tool"], "web_search")
        self.assertEqual(json.loads(body["input"])["query"], "hi")
        self.assertIn("rate limited", body["output"])
        # Start = end (00:02.000) minus 1.5s duration = 00:00.500, not zero-width.
        self.assertLess(body["startTime"], body["endTime"])

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


class TestTransport(unittest.TestCase):
    def test_transient_network_error_retries_before_succeeding(self):
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def getcode(self):
                return 200

            def read(self):
                return b"{}"

        with (
            mock.patch.object(
                hook.urllib.request,
                "urlopen",
                side_effect=[urllib.error.URLError("temporary"), Response()],
            ) as urlopen,
            mock.patch.object(hook.time, "sleep"),
        ):
            accepted = hook.post_batch(
                [{"id": "event-1", "type": "trace-create", "body": {}}],
                "https://example.test",
                "oc_test",
                {},
            )

        self.assertTrue(accepted)
        self.assertEqual(urlopen.call_count, 2)


class TestSelfTestEntrypoint(unittest.TestCase):
    def test_run_self_test_returns_zero(self):
        self.assertEqual(hook.run_self_test(), 0)


class TestStopHookEntrypoint(unittest.TestCase):
    def test_first_seen_completed_turn_creates_observations(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rollout = root / f"rollout-{THREAD_ID}.jsonl"
            state_dir = root / "state"
            turn_id = "turn-already-complete"
            rows = [
                {"timestamp": "2026-07-12T00:00:00Z", "type": "event_msg", "payload": {"type": "task_started", "turn_id": turn_id}},
                {"timestamp": "2026-07-12T00:00:01Z", "type": "event_msg", "payload": {"type": "user_message", "message": "hello"}},
                {"timestamp": "2026-07-12T00:00:03Z", "type": "event_msg", "payload": {"type": "task_complete", "turn_id": turn_id, "last_agent_message": "done"}},
            ]
            rollout.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
            delivered = []

            with (
                mock.patch.object(hook, "STATE_DIR", state_dir),
                mock.patch.object(hook, "STATE_FILE", state_dir / "state.json"),
                mock.patch.object(hook, "LOCK_FILE", state_dir / "state.lock"),
                mock.patch.object(hook, "resolve_config", return_value=("https://example.test", "oc_test", None)),
                mock.patch.object(hook, "deliver", side_effect=lambda events, *_: delivered.extend(events) or {0}),
                mock.patch.object(sys, "stdin", io.StringIO(json.dumps({"session_id": THREAD_ID, "transcript_path": str(rollout)}))),
            ):
                hook.main(["one_signal_codex_hook.py"])

            root_span = next(
                event for event in delivered
                if event["body"].get("id") == f"{THREAD_ID}-t1-root"
            )
            state_entries = [value for key, value in json.loads((state_dir / "state.json").read_text()).items() if key != "_thread_paths"]
            self.assertEqual(root_span["type"], "observation-create")
            self.assertLess(root_span["body"]["startTime"], root_span["body"]["endTime"])
            self.assertEqual(state_entries[0]["partial_turn_ids"], [])

    def test_in_progress_turn_uploads_without_advancing_checkpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rollout = root / f"rollout-{THREAD_ID}.jsonl"
            state_dir = root / "state"
            turn_id = "turn-in-progress"
            rows = [
                {"timestamp": "2026-07-12T00:00:00Z", "type": "event_msg", "payload": {"type": "task_started", "turn_id": turn_id}},
                {"timestamp": "2026-07-12T00:00:01Z", "type": "event_msg", "payload": {"type": "user_message", "message": "hello"}},
                {"timestamp": "2026-07-12T00:00:02Z", "type": "response_item", "payload": {
                    "type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "working"}],
                    "internal_chat_message_metadata_passthrough": {"turn_id": turn_id},
                }},
            ]
            rollout.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
            delivered = []
            payload = json.dumps({"session_id": THREAD_ID, "transcript_path": str(rollout)})

            with (
                mock.patch.object(hook, "STATE_DIR", state_dir),
                mock.patch.object(hook, "STATE_FILE", state_dir / "state.json"),
                mock.patch.object(hook, "LOCK_FILE", state_dir / "state.lock"),
                mock.patch.object(hook, "resolve_config", return_value=("https://example.test", "oc_test", None)),
                mock.patch.object(hook, "deliver", side_effect=lambda events, *_: delivered.extend(events) or {0}),
                mock.patch.object(sys, "stdin", io.StringIO(payload)),
            ):
                result = hook.main(["one_signal_codex_hook.py"])

            trace = next(event for event in delivered if event["type"] == "trace-create")
            root_span = next(
                event for event in delivered
                if event["body"].get("id") == f"{THREAD_ID}-t1-root"
            )
            partial_observations = [event for event in delivered if event["type"].startswith("observation-")]
            state_entries = [value for key, value in json.loads((state_dir / "state.json").read_text()).items() if key != "_thread_paths"]
            self.assertEqual(result, 0)
            self.assertEqual(trace["body"]["id"], f"{THREAD_ID}-t1")
            self.assertEqual(trace["body"]["metadata"]["completed"], False)
            self.assertTrue(all(event["type"] == "observation-create" for event in partial_observations))
            self.assertEqual(state_entries[0]["offset"], 0)
            self.assertEqual(state_entries[0]["turn_count"], 0)
            self.assertEqual(state_entries[0]["partial_turn_ids"], [turn_id])

            complete = {
                "timestamp": "2026-07-12T00:00:03Z",
                "type": "event_msg",
                "payload": {"type": "task_complete", "turn_id": turn_id, "last_agent_message": "done"},
            }
            with rollout.open("a", encoding="utf-8") as output:
                output.write(json.dumps(complete) + "\n")
            finalized = []
            with (
                mock.patch.object(hook, "STATE_DIR", state_dir),
                mock.patch.object(hook, "STATE_FILE", state_dir / "state.json"),
                mock.patch.object(hook, "LOCK_FILE", state_dir / "state.lock"),
                mock.patch.object(hook, "resolve_config", return_value=("https://example.test", "oc_test", None)),
                mock.patch.object(hook, "deliver", side_effect=lambda events, *_: finalized.extend(events) or {0}),
                mock.patch.object(sys, "stdin", io.StringIO(payload)),
            ):
                hook.main(["one_signal_codex_hook.py"])

            final_trace = next(event for event in finalized if event["type"] == "trace-create")
            final_root_span = next(
                event for event in finalized
                if event["body"].get("id") == root_span["body"]["id"]
            )
            final_observations = [event for event in finalized if event["type"].startswith("observation-")]
            final_entries = [value for key, value in json.loads((state_dir / "state.json").read_text()).items() if key != "_thread_paths"]
            self.assertEqual(final_trace["body"]["id"], trace["body"]["id"])
            self.assertEqual(final_trace["body"]["metadata"]["completed"], True)
            self.assertEqual(
                {event["body"]["id"] for event in final_observations},
                {event["body"]["id"] for event in partial_observations},
            )
            self.assertTrue(all(event["type"] == "observation-update" for event in final_observations))
            self.assertLess(final_root_span["body"]["startTime"], final_root_span["body"]["endTime"])
            self.assertGreater(final_entries[0]["offset"], 0)
            self.assertEqual(final_entries[0]["turn_count"], 1)
            self.assertEqual(final_entries[0]["partial_turn_ids"], [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
