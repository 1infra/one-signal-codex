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

import copy
import hashlib
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

    def test_uploads_only_codex_instructions_and_preserves_symlink_identity(self):
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
            (project / "CLAUDE.md").symlink_to("AGENTS.md")
            (nested / "AGENTS.md").write_text("nested agents", encoding="utf-8")
            turn = self.turns[0]
            turn.cwd = str(nested)

            with mock.patch.object(hook, "CODEX_HOME", home / ".codex"), mock.patch.object(hook.Path, "home", return_value=home):
                events = hook.build_turn_events(THREAD_ID, 1, turn, FIXTURE)

            trace = next(event for event in events if event["type"] == "trace-create")
            documents = trace["body"]["metadata"]["instruction_documents"]
            self.assertEqual([document["path"] for document in documents], [
                "~/.codex/AGENTS.md",
                "AGENTS.md",
                "packages/app/AGENTS.md",
            ])
            self.assertEqual(documents[1], {
                "agent": "codex",
                "path": "AGENTS.md",
                "scope": "project",
                "directory_scope": ".",
                "content": "project agents",
                "content_hash": hashlib.sha256(b"project agents").hexdigest(),
            })

    def test_instruction_snapshot_hashes_raw_content_but_otlp_contains_only_redacted_content(self):
        secret = "sk-" + ("A" * 20)
        raw_content = f"Use token={secret} for local testing"
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            project = home / "project"
            (project / ".git").mkdir(parents=True)
            (home / ".codex").mkdir()
            (project / "AGENTS.md").write_text(raw_content, encoding="utf-8")
            turn = copy.deepcopy(self.turns[0])
            turn.cwd = str(project)

            with (
                mock.patch.object(hook, "CODEX_HOME", home / ".codex"),
                mock.patch.object(hook.Path, "home", return_value=home),
            ):
                events = hook.build_turn_events(THREAD_ID, 1, turn, FIXTURE)

        spans = [span for span, _source in hook._classic_events_to_otlp(events)]
        root = next(span for span in spans if "parentSpanId" not in span)
        attributes = {
            entry["key"]: entry["value"] for entry in root["attributes"]
        }
        metadata = json.loads(attributes["one.signal.metadata"]["stringValue"])
        document = metadata["instruction_documents"][0]
        self.assertEqual(
            document["content_hash"],
            hashlib.sha256(raw_content.encode("utf-8")).hexdigest(),
        )
        self.assertEqual(
            document["content"],
            "Use token=<REDACTED:openai> for local testing",
        )
        self.assertNotIn(secret, json.dumps(spans))

    def test_later_turn_uploads_only_new_nested_instruction_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            project = home / "project"
            nested = project / "packages" / "app"
            (project / ".git").mkdir(parents=True)
            nested.mkdir(parents=True)
            (home / ".codex").mkdir()
            (project / "AGENTS.md").write_text("root rules", encoding="utf-8")
            (nested / "AGENTS.md").write_text("nested rules", encoding="utf-8")
            first = copy.deepcopy(self.turns[0])
            first.cwd = str(project)
            later = copy.deepcopy(self.turns[0])
            later.cwd = str(project)
            later.rounds[0].tool_calls = [{
                "call_id": "touch-nested",
                "name": "exec_command",
                "input": json.dumps({"cmd": "pwd", "workdir": str(nested)}),
            }]
            known: set[str] = set()

            with mock.patch.object(hook, "CODEX_HOME", home / ".codex"), mock.patch.object(hook.Path, "home", return_value=home):
                first_events = hook.build_turn_events(THREAD_ID, 1, first, FIXTURE, known_instruction_documents=known)
                later_events = hook.build_turn_events(THREAD_ID, 2, later, FIXTURE, known_instruction_documents=known)

            first_documents = next(event for event in first_events if event["type"] == "trace-create")["body"]["metadata"]["instruction_documents"]
            later_documents = next(event for event in later_events if event["type"] == "trace-create")["body"]["metadata"]["instruction_documents"]
            self.assertEqual([document["path"] for document in first_documents], ["AGENTS.md"])
            self.assertEqual([document["path"] for document in later_documents], ["packages/app/AGENTS.md"])

    def test_records_images_omitted_from_session_text(self):
        turn_id = "turn-with-image"
        rows = [
            ({"timestamp": "2026-07-12T00:00:00Z", "type": "event_msg", "payload": {"type": "task_started", "turn_id": turn_id}}, 1),
            ({"timestamp": "2026-07-12T00:00:01Z", "type": "response_item", "payload": {
                "type": "message", "role": "user",
                "content": [{"type": "input_text", "text": "inspect this"}, {"type": "input_image", "image_url": "data:image/png;base64,abc"}],
                "internal_chat_message_metadata_passthrough": {"turn_id": turn_id},
            }}, 2),
            ({"timestamp": "2026-07-12T00:00:02Z", "type": "event_msg", "payload": {"type": "user_message", "message": "inspect this"}}, 3),
            ({"timestamp": "2026-07-12T00:00:03Z", "type": "event_msg", "payload": {"type": "task_complete", "turn_id": turn_id, "last_agent_message": "done"}}, 4),
        ]

        turn = hook.build_turns(rows)[0]
        events = hook.build_turn_events(THREAD_ID, 1, turn, FIXTURE)
        trace = next(event for event in events if event["type"] == "trace-create")
        self.assertEqual(trace["body"]["metadata"]["omitted_image_count"], 1)

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

    def test_failed_exec_tool_span_emits_observation_level_error(self):
        # Nonzero structured exit_code must surface as Langfuse observation
        # level=ERROR so the server's errorRate metric is non-zero for failures.
        turn_id = "turn-exec-level-error"
        meta = {"turn_id": turn_id}
        rows = [
            ({"timestamp": "2026-07-12T00:00:00Z", "type": "event_msg", "payload": {"type": "task_started", "turn_id": turn_id}}, 1),
            ({"timestamp": "2026-07-12T00:00:01Z", "type": "response_item", "payload": {"type": "function_call", "name": "exec", "call_id": "fail", "arguments": "{}", "internal_chat_message_metadata_passthrough": meta}}, 2),
            ({"timestamp": "2026-07-12T00:00:02Z", "type": "response_item", "payload": {"type": "function_call_output", "call_id": "fail", "output": "{\"exit_code\":1,\"output\":\"boom\"}", "internal_chat_message_metadata_passthrough": meta}}, 3),
            ({"timestamp": "2026-07-12T00:00:03Z", "type": "event_msg", "payload": {"type": "task_complete", "turn_id": turn_id}}, 4),
        ]

        turn = hook.build_turns(rows)[0]
        events = hook.build_turn_events(THREAD_ID, 1, turn, FIXTURE)
        body = next(
            event["body"] for event in events
            if (event["body"].get("metadata") or {}).get("tool_id") == "fail"
        )

        self.assertEqual(body["metadata"]["exit_code"], 1)
        self.assertEqual(body["metadata"]["result_status"], "error")
        self.assertEqual(body["level"], "ERROR")

    def test_successful_exec_tool_span_omits_observation_level(self):
        # Success must not send level at all (not even DEFAULT) so only real
        # failures contribute to Langfuse errorRate.
        turn_id = "turn-exec-level-ok"
        meta = {"turn_id": turn_id}
        rows = [
            ({"timestamp": "2026-07-12T00:00:00Z", "type": "event_msg", "payload": {"type": "task_started", "turn_id": turn_id}}, 1),
            ({"timestamp": "2026-07-12T00:00:01Z", "type": "response_item", "payload": {"type": "function_call", "name": "exec", "call_id": "ok", "arguments": "{}", "internal_chat_message_metadata_passthrough": meta}}, 2),
            ({"timestamp": "2026-07-12T00:00:02Z", "type": "response_item", "payload": {"type": "function_call_output", "call_id": "ok", "output": "{\"exit_code\":0,\"output\":\"done\"}", "internal_chat_message_metadata_passthrough": meta}}, 3),
            ({"timestamp": "2026-07-12T00:00:03Z", "type": "event_msg", "payload": {"type": "task_complete", "turn_id": turn_id}}, 4),
        ]

        turn = hook.build_turns(rows)[0]
        events = hook.build_turn_events(THREAD_ID, 1, turn, FIXTURE)
        body = next(
            event["body"] for event in events
            if (event["body"].get("metadata") or {}).get("tool_id") == "ok"
        )

        self.assertEqual(body["metadata"]["exit_code"], 0)
        self.assertEqual(body["metadata"]["result_status"], "success")
        self.assertNotIn("level", body)

    def test_mcp_error_tool_span_emits_observation_level_error(self):
        # MCP Err results set result_status=error with no exit_code; the tool
        # SPAN must still carry level=ERROR for errorRate accounting.
        turn_id = "turn-mcp-level-error"
        rows = [
            ({"timestamp": "2026-07-12T00:00:00Z", "type": "event_msg", "payload": {"type": "task_started", "turn_id": turn_id}}, 1),
            ({"timestamp": "2026-07-12T00:00:01Z", "type": "event_msg", "payload": {
                "type": "mcp_tool_call_end",
                "call_id": "mcp-level-error",
                "invocation": {"server": "github", "tool": "search", "arguments": {}},
                "result": {"Err": {"content": [{"type": "text", "text": "denied"}]}},
            }}, 2),
            ({"timestamp": "2026-07-12T00:00:02Z", "type": "event_msg", "payload": {"type": "task_complete", "turn_id": turn_id}}, 3),
        ]

        turn = hook.build_turns(rows)[0]
        events = hook.build_turn_events(THREAD_ID, 1, turn, FIXTURE)
        body = next(
            event["body"] for event in events
            if (event["body"].get("metadata") or {}).get("tool_id") == "mcp-level-error"
        )

        self.assertEqual(body["metadata"]["result_status"], "error")
        self.assertEqual(body["level"], "ERROR")

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

    def _observations_by_kind(self, kind):
        # Typed envelopes omit body.type; kind is implied by envelope type.
        envelopes = {
            "SPAN": ("span-create", "span-update"),
            "EVENT": ("event-create",),
            "GENERATION": ("generation-create", "generation-update"),
        }
        return [e for e in self.events if e["type"] in envelopes.get(kind, ())]

    def test_exactly_one_trace_create(self):
        self.assertEqual(len(self._by_type("trace-create")), 1)

    def test_no_classic_observation_envelopes(self):
        # Classic observation-create/update drop body.environment and body.model
        # (verified 2026-07-20 production Langfuse 4-way matrix). Every obs in
        # a built batch must use a typed envelope.
        classic = [
            e for e in self.events
            if e["type"] in ("observation-create", "observation-update")
        ]
        self.assertEqual(classic, [], [e["type"] for e in classic])

    def test_trace_id_and_name_deterministic(self):
        trace = self._by_type("trace-create")[0]
        self.assertEqual(trace["body"]["id"], f"{THREAD_ID}-t1")
        self.assertIn("Codex CLI - Turn 1", trace["body"]["name"])

    def test_root_span_present(self):
        spans = [e for e in self._observations_by_kind("SPAN") if e["body"]["name"] == "Turn 1"]
        self.assertEqual(len(spans), 1)
        self.assertIsNone(spans[0]["body"].get("parentObservationId"))
        # Root SPAN uses span-create (typed); body.type is implied by envelope.
        self.assertEqual(spans[0]["type"], "span-create")
        self.assertNotIn("type", spans[0]["body"])

    def test_generation_present_with_model_and_usage(self):
        # generation-create is required so Langfuse populates providedModelName;
        # classic observation-create stores body.model but leaves that metrics
        # dim null (and also drops environment).
        generations = self._by_type("generation-create")
        self.assertGreaterEqual(len(generations), 1)
        gen = generations[0]
        self.assertEqual(gen["type"], "generation-create")
        self.assertNotIn("type", gen["body"])
        self.assertEqual(gen["body"]["model"], "gpt-test-model")
        usage = gen["body"]["usageDetails"]
        self.assertIsNotNone(usage)
        # input_tokens=100 includes cached_input_tokens=10; reported input is
        # the uncached remainder (see usage_details_from_token_count, #130).
        self.assertEqual(usage.get("input"), 90)
        self.assertEqual(usage.get("output"), 20)
        self.assertEqual(usage.get("cache_read_input_tokens"), 10)
        self.assertEqual(usage.get("reasoning_output_tokens"), 5)

    def test_tool_span_carries_metadata_tool_name(self):
        tool_spans = [
            e for e in self._observations_by_kind("SPAN")
            if "tool_name" in (e["body"].get("metadata") or {})
        ]
        # The fixture yields exactly two tool spans: the shell call and the
        # MCP call. An exact count guards against duplicate or orphan spans.
        self.assertEqual(len(tool_spans), 2)
        span = next(s for s in tool_spans if s["body"]["metadata"]["tool_name"] == "shell")
        self.assertEqual(span["body"]["metadata"]["tool_id"], "call_fixture_1")
        self.assertIn("hi", span["body"]["output"])
        # Tool calls emit as SPAN via span-create (there is no "TOOL" envelope);
        # classic ingestion would also reject a literal "TOOL" body.type.
        self.assertEqual(span["type"], "span-create")
        self.assertNotIn("type", span["body"])

    def test_skill_injection_is_aggregated_on_trace(self):
        trace = self._by_type("trace-create")[0]
        self.assertEqual(trace["body"]["metadata"]["skill_names"], ["code-review"])
        self.assertIn("skill:code-review", trace["body"]["tags"])
        # The assistant-role message that merely quotes `<skill><name>...`
        # markup must never be counted as an invocation.
        self.assertNotIn("skill:not-invoked", trace["body"]["tags"])

    def test_mcp_call_is_emitted_as_attributed_tool_span(self):
        spans = [
            e for e in self._observations_by_kind("SPAN")
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
            if e["type"] in ("span-create", "span-update")
            and (e["body"].get("metadata") or {}).get("mcp_server") == "exa"
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
            e for e in self._observations_by_kind("EVENT") if e["body"]["name"] == "Reasoning"
        ]
        self.assertEqual(len(reasoning_events), 1)
        self.assertEqual(reasoning_events[0]["type"], "event-create")
        self.assertNotIn("type", reasoning_events[0]["body"])
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
    @staticmethod
    def events():
        return [
            {
                "id": "trace-envelope",
                "timestamp": "2026-07-12T00:00:00.000Z",
                "type": "trace-create",
                "body": {
                    "id": "trace-a",
                    "timestamp": "2026-07-12T00:00:00.000Z",
                    "name": "Codex CLI - Turn 1",
                    "sessionId": "thread-a",
                    "userId": "configured-codex-user",
                    "metadata": {"source": "codex-cli"},
                    "tags": ["codex-cli"],
                },
            },
            {
                "id": "root-envelope",
                "timestamp": "2026-07-12T00:00:01.000Z",
                "type": "span-create",
                "body": {
                    "id": "root-a",
                    "traceId": "trace-a",
                    "name": "Turn 1",
                    "startTime": "2026-07-12T00:00:00.000Z",
                    "endTime": "2026-07-12T00:00:01.000Z",
                },
            },
            {
                "id": "gen-envelope",
                "timestamp": "2026-07-12T00:00:01.000Z",
                "type": "generation-create",
                "body": {
                    "id": "gen-a",
                    "traceId": "trace-a",
                    "parentObservationId": "root-a",
                    "name": "Codex Generation 1",
                    "startTime": "2026-07-12T00:00:00.000Z",
                    "endTime": "2026-07-12T00:00:01.000Z",
                    "model": "gpt-test",
                    "usageDetails": {
                        "input": 12,
                        "output": 3,
                        "reasoning_output_tokens": 2,
                    },
                    "metadata": {"provider": "openai"},
                },
            },
        ]

    def test_posts_otlp_json_with_basic_auth(self):
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def getcode(self):
                return 200

            def read(self):
                return b"{}"

        with mock.patch.object(
            hook.urllib.request,
            "urlopen",
            return_value=Response(),
        ) as urlopen:
            accepted = hook.post_batch(
                self.events(),
                "https://example.test",
                "oc_test",
                {"sdk_name": "one-signal-codex-hook"},
            )

        self.assertTrue(accepted)
        request = urlopen.call_args.args[0]
        self.assertEqual(
            request.full_url,
            "https://example.test/api/public/otel/v1/traces",
        )
        self.assertEqual(request.get_header("Authorization"), "Basic b2NfdGVzdDo=")
        payload = json.loads(request.data)
        self.assertNotIn("batch", payload)
        spans = payload["resourceSpans"][0]["scopeSpans"][0]["spans"]
        self.assertEqual(len(spans), 2)
        self.assertEqual(spans[0]["traceId"], "d5b56921fb1155c8e07a80bcfae224b5")
        self.assertEqual(spans[1]["parentSpanId"], "0a16c2d64ed82e34")
        root_attributes = {
            entry["key"]: entry["value"] for entry in spans[0]["attributes"]
        }
        self.assertEqual(
            root_attributes["one.signal.configured_user_id"]["stringValue"],
            "configured-codex-user",
        )
        self.assertNotIn("user.id", root_attributes)
        generation_attributes = {
            entry["key"]: entry["value"] for entry in spans[1]["attributes"]
        }
        self.assertEqual(
            generation_attributes["gen_ai.system"]["stringValue"],
            "openai",
        )
        self.assertEqual(
            generation_attributes["gen_ai.usage.reasoning_tokens"]["intValue"],
            "2",
        )
        self.assertEqual(
            generation_attributes["one.signal.agent"]["stringValue"],
            "codex-cli",
        )

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
                self.events(),
                "https://example.test",
                "oc_test",
                {},
            )

        self.assertTrue(accepted)
        self.assertEqual(urlopen.call_count, 2)

    def test_permanent_4xx_is_not_retried_or_acknowledged(self):
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def getcode(self):
                return 400

            def read(self):
                return b'{"error":"invalid"}'

        with mock.patch.object(
            hook.urllib.request,
            "urlopen",
            return_value=Response(),
        ) as urlopen:
            accepted = hook.post_batch(
                self.events(),
                "https://example.test",
                "oc_test",
                {},
            )

        self.assertFalse(accepted)
        self.assertEqual(urlopen.call_count, 1)

    def test_chunks_exact_otlp_json_by_span_count_and_final_bytes(self):
        metadata = {"sdk_name": "one-signal-codex-hook"}
        span = {
            "traceId": "0" * 32,
            "spanId": "0" * 16,
            "name": "x",
            "kind": 1,
            "startTimeUnixNano": "1",
            "endTimeUnixNano": "2",
            "attributes": [],
        }
        groups = hook._chunk_span_indices([span] * 201, metadata)
        self.assertEqual([len(group) for group in groups], [200, 1])

        two_span_bytes = len(hook._otlp_payload([span, span], metadata))
        groups = hook._chunk_span_indices(
            [span, span],
            metadata,
            max_bytes=two_span_bytes - 1,
        )
        self.assertEqual(groups, [[0], [1]])
        self.assertTrue(
            all(
                len(hook._otlp_payload([span for _ in group], metadata))
                <= two_span_bytes - 1
                for group in groups
            )
        )

        with self.assertRaisesRegex(ValueError, "single OTLP span"):
            hook._chunk_span_indices([span], metadata, max_bytes=1)


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
            self.assertEqual(root_span["type"], "span-create")
            self.assertNotIn("type", root_span["body"])
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
            # Creates: SPAN → span-create; GENERATION → generation-create;
            # EVENT → event-create. Classic observation-* must not appear.
            typed_create = ("span-create", "generation-create", "event-create")
            partial_observations = [
                event for event in delivered if event["type"] in typed_create
            ]
            state_entries = [value for key, value in json.loads((state_dir / "state.json").read_text()).items() if key != "_thread_paths"]
            self.assertEqual(result, 0)
            self.assertEqual(trace["body"]["id"], f"{THREAD_ID}-t1")
            self.assertEqual(trace["body"]["metadata"]["completed"], False)
            self.assertTrue(
                any(event["type"] == "generation-create" for event in partial_observations)
            )
            self.assertTrue(
                any(event["type"] == "span-create" for event in partial_observations)
            )
            self.assertFalse(
                any(
                    event["type"] in ("observation-create", "observation-update")
                    for event in delivered
                )
            )
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
            # Updates use typed *-update envelopes (EVENT re-emits event-create).
            typed_update = ("span-update", "generation-update", "event-create")
            final_observations = [
                event for event in finalized if event["type"] in typed_update
            ]
            final_entries = [value for key, value in json.loads((state_dir / "state.json").read_text()).items() if key != "_thread_paths"]
            self.assertEqual(final_trace["body"]["id"], trace["body"]["id"])
            self.assertEqual(final_trace["body"]["metadata"]["completed"], True)
            self.assertEqual(
                {event["body"]["id"] for event in final_observations},
                {event["body"]["id"] for event in partial_observations},
            )
            self.assertTrue(
                all(event["type"] in typed_update for event in final_observations)
            )
            self.assertFalse(
                any(
                    event["type"] in ("observation-create", "observation-update")
                    for event in finalized
                )
            )
            self.assertEqual(final_root_span["type"], "span-update")
            self.assertLess(final_root_span["body"]["startTime"], final_root_span["body"]["endTime"])
            self.assertGreater(final_entries[0]["offset"], 0)
            self.assertEqual(final_entries[0]["turn_count"], 1)
            self.assertEqual(final_entries[0]["partial_turn_ids"], [])


class TestRedactText(unittest.TestCase):
    """Pre-upload secret redaction (plugin-side, before ingest).

    Covers known-format provider tokens, credentialed URIs, negatives,
    idempotency, and one end-to-end path through build_turn_events.
    """

    # --- Positive: each known token class gets the right short tag ---

    def test_aws_access_key_id(self):
        raw = "creds AKIAIOSFODNN7EXAMPLE extra"
        out = hook.redact_text(raw)
        self.assertIn("<REDACTED:aws>", out)
        self.assertNotIn("AKIAIOSFODNN7EXAMPLE", out)

    def test_github_pat_and_classic_token(self):
        ghp = "ghp_" + ("a" * 36)
        fine = "github_pat_" + ("b" * 22)
        out = hook.redact_text(f"tokens {ghp} and {fine}")
        self.assertEqual(out.count("<REDACTED:github>"), 2)
        self.assertNotIn(ghp, out)
        self.assertNotIn(fine, out)

    def test_openai_style_sk_token(self):
        tok = "sk-" + ("A" * 20)
        out = hook.redact_text(f"key={tok}")
        self.assertIn("<REDACTED:openai>", out)
        self.assertNotIn(tok, out)

    def test_slack_token(self):
        tok = "xoxb-" + ("1" * 10)
        out = hook.redact_text(f"auth {tok}")
        self.assertIn("<REDACTED:slack>", out)
        self.assertNotIn(tok, out)

    def test_google_api_key(self):
        tok = "AIza" + ("C" * 35)
        out = hook.redact_text(tok)
        self.assertEqual(out, "<REDACTED:google>")

    def test_stripe_live_and_test_keys(self):
        live = "sk_live_" + ("d" * 16)
        test = "rk_test_" + ("e" * 16)
        out = hook.redact_text(f"{live} {test}")
        self.assertEqual(out.count("<REDACTED:stripe>"), 2)
        self.assertNotIn(live, out)
        self.assertNotIn(test, out)

    # --- Expanded SaaS tokens (betterleaks / gitleaks shapes; FAKE only) ---

    def test_anthropic_sk_ant_before_generic_sk(self):
        tok = "sk-ant-api03-" + ("A" * 20)
        out = hook.redact_text(f"key={tok}")
        self.assertIn("<REDACTED:anthropic>", out)
        self.assertNotIn(tok, out)
        self.assertNotIn("<REDACTED:openai>", out)

    def test_openrouter_sk_or_before_generic_sk(self):
        tok = "sk-or-v1-" + ("a" * 64)
        out = hook.redact_text(f"key={tok}")
        self.assertIn("<REDACTED:openrouter>", out)
        self.assertNotIn(tok, out)
        self.assertNotIn("<REDACTED:openai>", out)

    def test_figma_token(self):
        tok = "figd_" + ("A" * 40)
        out = hook.redact_text(f"token {tok}")
        self.assertIn("<REDACTED:figma>", out)
        self.assertNotIn(tok, out)

    def test_npm_token(self):
        tok = "npm_" + ("a" * 36)
        out = hook.redact_text(f"auth {tok}")
        self.assertIn("<REDACTED:npm>", out)
        self.assertNotIn(tok, out)

    def test_gitlab_pat(self):
        tok = "glpat-" + ("x" * 20)
        out = hook.redact_text(f"GITLAB={tok}")
        self.assertIn("<REDACTED:gitlab>", out)
        self.assertNotIn(tok, out)

    def test_huggingface_token(self):
        tok = "hf_" + ("a" * 34)
        out = hook.redact_text(f"Bearer {tok}")
        self.assertIn("<REDACTED:huggingface>", out)
        self.assertNotIn(tok, out)

    def test_supabase_sbp_and_sb_secret(self):
        sbp = "sbp_" + ("a" * 40)
        secret = "sb_secret_" + ("b" * 31)
        out = hook.redact_text(f"{sbp} {secret}")
        self.assertEqual(out.count("<REDACTED:supabase>"), 2)
        self.assertNotIn(sbp, out)
        self.assertNotIn(secret, out)

    def test_shopify_tokens(self):
        tok = "shpat_" + ("a" * 32)
        out = hook.redact_text(tok)
        self.assertEqual(out, "<REDACTED:shopify>")

    def test_digitalocean_tokens(self):
        tok = "dop_v1_" + ("a" * 64)
        out = hook.redact_text(tok)
        self.assertEqual(out, "<REDACTED:digitalocean>")

    def test_databricks_token(self):
        tok = "dapi" + ("a" * 32)
        out = hook.redact_text(f"token={tok}")
        self.assertIn("<REDACTED:databricks>", out)
        self.assertNotIn(tok, out)

    def test_sendgrid_token(self):
        tok = "SG." + ("A" * 66)
        out = hook.redact_text(tok)
        self.assertEqual(out, "<REDACTED:sendgrid>")

    def test_telegram_bot_token(self):
        tok = "1234567890:A" + ("A" * 34)
        out = hook.redact_text(f"bot {tok}")
        self.assertIn("<REDACTED:telegram>", out)
        self.assertNotIn(tok, out)

    def test_airtable_pat(self):
        tok = "pat" + ("A" * 14) + "." + ("a" * 64)
        out = hook.redact_text(tok)
        self.assertEqual(out, "<REDACTED:airtable>")

    def test_grafana_glc_and_glsa(self):
        glc = "glc_" + ("A" * 40)
        glsa = "glsa_" + ("A" * 32) + "_" + ("a" * 8)
        out = hook.redact_text(f"{glc} {glsa}")
        self.assertEqual(out.count("<REDACTED:grafana>"), 2)
        self.assertNotIn(glc, out)
        self.assertNotIn(glsa, out)

    def test_sentry_org_token(self):
        tok = "sntrys_" + ("A" * 50)
        out = hook.redact_text(tok)
        self.assertEqual(out, "<REDACTED:sentry>")

    def test_fly_fo1_token(self):
        tok = "fo1_" + ("a" * 43)
        out = hook.redact_text(tok)
        self.assertEqual(out, "<REDACTED:fly>")

    def test_groq_token(self):
        tok = "gsk_" + ("A" * 52)
        out = hook.redact_text(tok)
        self.assertEqual(out, "<REDACTED:groq>")

    def test_xai_token(self):
        tok = "xai-" + ("A" * 80)
        out = hook.redact_text(tok)
        self.assertEqual(out, "<REDACTED:xai>")

    def test_perplexity_token(self):
        tok = "pplx-" + ("A" * 48)
        out = hook.redact_text(tok)
        self.assertEqual(out, "<REDACTED:perplexity>")

    def test_replicate_token(self):
        tok = "r8_" + ("A" * 37)
        out = hook.redact_text(tok)
        self.assertEqual(out, "<REDACTED:replicate>")

    def test_doppler_token(self):
        tok = "dp.pt." + ("a" * 43)
        out = hook.redact_text(tok)
        self.assertEqual(out, "<REDACTED:doppler>")

    def test_linear_token(self):
        tok = "lin_api_" + ("a" * 40)
        out = hook.redact_text(tok)
        self.assertEqual(out, "<REDACTED:linear>")

    def test_notion_token(self):
        tok = "ntn_" + ("1" * 11) + ("A" * 35)
        out = hook.redact_text(tok)
        self.assertEqual(out, "<REDACTED:notion>")

    def test_postman_token(self):
        tok = "PMAK-" + ("a" * 24) + "-" + ("b" * 34)
        out = hook.redact_text(tok)
        self.assertEqual(out, "<REDACTED:postman>")

    def test_onepassword_ops_token(self):
        tok = "ops_eyJ" + ("A" * 250)
        out = hook.redact_text(tok)
        self.assertEqual(out, "<REDACTED:1password>")

    def test_vercel_token(self):
        tok = "vcp_" + ("A" * 56)
        out = hook.redact_text(tok)
        self.assertEqual(out, "<REDACTED:vercel>")

    def test_jwt(self):
        # Three base64url segments; header typically starts with eyJ
        jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.signaturexx"
        out = hook.redact_text(f"Bearer {jwt}")
        self.assertIn("<REDACTED:jwt>", out)
        self.assertNotIn(jwt, out)

    def test_pem_private_key_block(self):
        pem = (
            "-----BEGIN RSA PRIVATE KEY-----\n"
            "MIIEowIBAAKCAQEA0Z3VS5JJcds3xfn/ygWyF6P\n"
            "-----END RSA PRIVATE KEY-----"
        )
        out = hook.redact_text(f"key material:\n{pem}\ndone")
        self.assertIn("<REDACTED:pem>", out)
        self.assertNotIn("BEGIN RSA PRIVATE KEY", out)
        self.assertNotIn("MIIEowIBAAKCAQEA", out)
        self.assertIn("key material:", out)
        self.assertIn("done", out)

    def test_db_connection_string_masks_only_password(self):
        raw = "postgres://alice:s3cret-pass@db.example.com:5432/app"
        out = hook.redact_text(raw)
        self.assertEqual(out, "postgres://alice:<REDACTED>@db.example.com:5432/app")
        self.assertNotIn("s3cret-pass", out)

    def test_mongodb_srv_and_redis_connection_strings(self):
        mongo = "mongodb+srv://user:p%40ss@cluster0.example.net/db"
        redis = "rediss://cache:hunter2@redis.internal:6380/0"
        out = hook.redact_text(f"{mongo} | {redis}")
        self.assertIn("mongodb+srv://user:<REDACTED>@cluster0.example.net/db", out)
        self.assertIn("rediss://cache:<REDACTED>@redis.internal:6380/0", out)
        self.assertNotIn("p%40ss", out)
        self.assertNotIn("hunter2", out)

    def test_generic_uri_with_embedded_password(self):
        raw = "fetch https://deploy:topsecret@ci.example.com/hook"
        out = hook.redact_text(raw)
        self.assertEqual(out, "fetch https://deploy:<REDACTED>@ci.example.com/hook")
        self.assertNotIn("topsecret", out)

    # --- Negatives: lookalikes and non-credential URLs stay intact ---

    def test_sk_lookalike_without_token_shape_unchanged(self):
        # Word-boundary + length bounds must not fire on ordinary prose /
        # skill-style names that merely contain the "sk-" substring.
        prose = "skill-name-with-sk-prefix is fine; also mysk-not-a-token"
        self.assertEqual(hook.redact_text(prose), prose)

    def test_sk_ant_lookalike_too_short_unchanged(self):
        # sk-ant- body must be ≥20 chars after the prefix.
        short = "sk-ant-tooshort"
        self.assertEqual(hook.redact_text(short), short)

    def test_sk_or_lookalike_wrong_shape_unchanged(self):
        # openrouter requires sk-or-v1- + exactly 64 hex; short/non-hex fall through.
        short = "sk-or-v1-nothex"
        # Not enough hex digits → not openrouter; too short for generic sk- either
        # if the total after "sk-" is < 20. Pad carefully so generic also misses.
        self.assertEqual(hook.redact_text(short), short)

    def test_figma_lookalike_too_short_unchanged(self):
        short = "figd_tooshort"
        self.assertEqual(hook.redact_text(short), short)

    def test_npm_lookalike_too_short_unchanged(self):
        short = "npm_short"
        self.assertEqual(hook.redact_text(short), short)

    def test_gitlab_lookalike_too_short_unchanged(self):
        short = "glpat-short"
        self.assertEqual(hook.redact_text(short), short)

    def test_plain_url_without_password_unchanged(self):
        url = "See https://example.com/path?q=1 and postgres://localhost/db"
        self.assertEqual(hook.redact_text(url), url)

    def test_aws_lookalike_wrong_length_unchanged(self):
        # AKIA must be followed by exactly 16 alnum chars.
        short = "AKIAIOSFODNN7EXAM"  # 15 after AKIA
        longish = "AKIAIOSFODNN7EXAMPLEX"  # 17 after AKIA
        self.assertEqual(hook.redact_text(short), short)
        self.assertEqual(hook.redact_text(longish), longish)

    # --- Idempotency: double-pass stable; never re-touch placeholders ---

    def test_idempotent_double_pass(self):
        raw = (
            "aws=AKIAIOSFODNN7EXAMPLE "
            "gh=ghp_" + ("x" * 36) + " "
            "db=postgres://u:pw@h/db "
            "https://a:b@host/z"
        )
        once = hook.redact_text(raw)
        twice = hook.redact_text(once)
        self.assertEqual(once, twice)
        self.assertIn("<REDACTED:aws>", once)
        self.assertIn("<REDACTED:github>", once)
        self.assertIn("postgres://u:<REDACTED>@h/db", once)
        self.assertIn("https://a:<REDACTED>@host/z", once)

    def test_existing_placeholder_not_reprocessed(self):
        # A placeholder must be left byte-for-byte alone even if its interior
        # would otherwise look token-like; neighboring secrets still redact.
        already = "prev <REDACTED:aws> mid AKIAIOSFODNN7EXAMPLE end"
        out = hook.redact_text(already)
        self.assertEqual(out, "prev <REDACTED:aws> mid <REDACTED:aws> end")

    # --- End-to-end: real pipeline masks secrets in tool span body ---

    def test_tool_span_body_redacts_secret_via_pipeline(self):
        turn_id = "turn-redact-e2e"
        meta = {"turn_id": turn_id}
        secret = "sk-" + ("Z" * 24)
        tool_out = f'{{"exit_code":0,"output":"token {secret} ok"}}'
        rows = [
            ({"timestamp": "2026-07-12T00:00:00Z", "type": "event_msg", "payload": {"type": "task_started", "turn_id": turn_id}}, 1),
            ({"timestamp": "2026-07-12T00:00:01Z", "type": "event_msg", "payload": {
                "type": "user_message", "message": f"use key {secret}",
            }}, 2),
            ({"timestamp": "2026-07-12T00:00:02Z", "type": "response_item", "payload": {
                "type": "function_call", "name": "shell", "call_id": "redact-1",
                "arguments": json.dumps({"command": f"echo {secret}"}),
                "internal_chat_message_metadata_passthrough": meta,
            }}, 3),
            ({"timestamp": "2026-07-12T00:00:03Z", "type": "response_item", "payload": {
                "type": "function_call_output", "call_id": "redact-1",
                "output": tool_out,
                "internal_chat_message_metadata_passthrough": meta,
            }}, 4),
            ({"timestamp": "2026-07-12T00:00:04Z", "type": "event_msg", "payload": {
                "type": "task_complete", "turn_id": turn_id,
                "last_agent_message": f"done with {secret}",
            }}, 5),
        ]

        turn = hook.build_turns(rows)[0]
        events = hook.build_turn_events(THREAD_ID, 1, turn, FIXTURE)
        tool_body = next(
            event["body"] for event in events
            if (event["body"].get("metadata") or {}).get("tool_id") == "redact-1"
        )
        trace = next(event["body"] for event in events if event["type"] == "trace-create")

        self.assertNotIn(secret, tool_body.get("input") or "")
        self.assertNotIn(secret, tool_body.get("output") or "")
        self.assertIn("<REDACTED:openai>", tool_body.get("input") or "")
        self.assertIn("<REDACTED:openai>", tool_body.get("output") or "")
        # User + final assistant free-text on the trace must also be masked.
        self.assertNotIn(secret, (trace.get("input") or {}).get("content") or "")
        self.assertNotIn(secret, (trace.get("output") or {}).get("content") or "")
        self.assertIn("<REDACTED:openai>", (trace.get("input") or {}).get("content") or "")


class TestPluginHookConfiguration(unittest.TestCase):
    def test_marketplace_local_source_uses_relative_path(self):
        config_path = Path(__file__).resolve().parent / ".agents" / "plugins" / "marketplace.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        source = config["plugins"][0]["source"]

        self.assertEqual(source, {"source": "local", "path": "./"})

    def test_stop_hook_resolves_entrypoint_from_plugin_root(self):
        config_path = Path(__file__).resolve().parent / "hooks" / "hooks.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        command = config["hooks"]["Stop"][0]["hooks"][0]["command"]

        self.assertEqual(command, 'python3 "${PLUGIN_ROOT}/one_signal_codex_hook.py"')
        self.assertNotIn("plugins/cache", command)


# Real 2026-07-20 e2e rollout: one aggregated custom_tool_call/exec that embeds
# four shell steps (apply_patch + cat + failing ls + echo). Exit 1 for
# `ls /nonexistent-directory-e2e-test` lives nested inside the JSON body of the
# custom_tool_call_output list, not at a top-level exit_code field.
FAILED_EXEC_FIXTURE = (
    Path(__file__).resolve().parent / "fixtures" / "rollout-failed-exec-e2e.jsonl"
)
FAILED_EXEC_THREAD = "019f7e1f-2aab-7801-906f-28bbca2a214f"


class TestRealFailedExecRollout(unittest.TestCase):
    def test_real_fixture_failing_exec_span_emits_level_error(self):
        self.assertTrue(FAILED_EXEC_FIXTURE.is_file(), f"missing fixture {FAILED_EXEC_FIXTURE}")
        rows = hook.read_new_lines(FAILED_EXEC_FIXTURE, 0)
        turns = hook.build_turns(rows)
        self.assertEqual(len(turns), 1)
        self.assertTrue(turns[0].complete)

        events = hook.build_turn_events(
            FAILED_EXEC_THREAD, 1, turns[0], FAILED_EXEC_FIXTURE
        )
        tool_spans = [
            event["body"]
            for event in events
            if event.get("type") in ("span-create", "span-update")
            and str(event["body"].get("name") or "").startswith("Tool:")
        ]
        # Real rollout ERROR tool span must use a typed envelope so level +
        # environment reach org-scoped reads (classic observation-create drops both).
        self.assertFalse(
            any(e["type"] in ("observation-create", "observation-update") for e in events)
        )
        tool_events = [
            event for event in events
            if event.get("type") in ("span-create", "span-update")
            and str(event["body"].get("name") or "").startswith("Tool:")
        ]
        self.assertTrue(all(e["type"] == "span-create" for e in tool_events))
        self.assertTrue(all("type" not in e["body"] for e in tool_events))

        # Bug 1b check: rollout records ONE aggregated exec custom_tool_call
        # (four cmds inside the JS script), not four separate tool calls — so
        # exactly one Tool span is correct, not a span-extraction miss.
        self.assertEqual(len(tool_spans), 1, [b.get("name") for b in tool_spans])
        body = tool_spans[0]
        self.assertEqual(body["name"], "Tool: exec")
        self.assertEqual(body["metadata"].get("exit_code"), 1)
        self.assertEqual(body["metadata"].get("result_status"), "error")
        self.assertEqual(body["level"], "ERROR")

        # Any non-failing tool SPANs (none in this fixture beyond the single
        # aggregated exec) must omit level entirely — assert the invariant on
        # all successful tool spans derived from the same turn assembly path.
        for span in tool_spans:
            if span is body:
                continue
            self.assertNotIn("level", span)

    def test_tool_call_exit_code_reads_nested_exec_mode_output(self):
        # Exact shape from rollout line 17: custom_tool_call_output list with a
        # preamble block + a JSON block whose nested expected_failure.exit_code=1.
        payload = {
            "type": "custom_tool_call_output",
            "call_id": "call_UtFQuUbAUqqIFsRHrX5iTI1v",
            "output": [
                {
                    "type": "input_text",
                    "text": "Script completed\nWall time 0.8 seconds\nOutput:\n",
                },
                {
                    "type": "input_text",
                    "text": (
                        '{"write":{},"cat":{"chunk_id":"dc33a5","exit_code":0,'
                        '"output":"ok"},"expected_failure":{"chunk_id":"5c63d0",'
                        '"exit_code":1,"output":"ls: No such file or directory\\n"},'
                        '"echo":{"chunk_id":"90ccd6","exit_code":0,"output":"done\\n"}}'
                    ),
                },
            ],
        }
        self.assertEqual(hook.tool_call_exit_code(payload), 1)


class TestCommitOnSuccessful207(unittest.TestCase):
    def test_successful_207_commits_completed_turn(self):
        # Fully-successful multi-status ingest (failed=0) must advance the
        # transcript checkpoint for a complete turn. Regression: prod logged
        # "ingest ok: status=207 ... failed=0" then "only 0/1 completed turn(s)
        # committed" when Stop raced task_complete / bookkeeping skipped accept.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_dir = root / "state"
            turn_id = "turn-commit-207"
            rows = [
                {
                    "timestamp": "2026-07-12T00:00:00Z",
                    "type": "event_msg",
                    "payload": {"type": "task_started", "turn_id": turn_id},
                },
                {
                    "timestamp": "2026-07-12T00:00:01Z",
                    "type": "event_msg",
                    "payload": {"type": "user_message", "message": "hello"},
                },
                {
                    "timestamp": "2026-07-12T00:00:03Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "task_complete",
                        "turn_id": turn_id,
                        "last_agent_message": "done",
                    },
                },
            ]
            rollout = root / f"rollout-{THREAD_ID}.jsonl"
            rollout.write_text(
                "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
            )

            class Response:
                def __enter__(self):
                    return self

                def __exit__(self, *_):
                    return False

                def getcode(self):
                    return 207

                def read(self):
                    return b'{"errors":[]}'

            with (
                mock.patch.object(hook, "STATE_DIR", state_dir),
                mock.patch.object(hook, "STATE_FILE", state_dir / "state.json"),
                mock.patch.object(hook, "LOCK_FILE", state_dir / "state.lock"),
                mock.patch.object(
                    hook,
                    "resolve_config",
                    return_value=("https://example.test", "oc_test", None),
                ),
                mock.patch.object(
                    hook.urllib.request, "urlopen", return_value=Response()
                ),
                mock.patch.object(
                    sys,
                    "stdin",
                    io.StringIO(
                        json.dumps(
                            {
                                "session_id": THREAD_ID,
                                "transcript_path": str(rollout),
                            }
                        )
                    ),
                ),
            ):
                result = hook.main(["one_signal_codex_hook.py"])

            self.assertEqual(result, 0)
            state_entries = [
                value
                for key, value in json.loads(
                    (state_dir / "state.json").read_text(encoding="utf-8")
                ).items()
                if key != "_thread_paths"
            ]
            self.assertEqual(len(state_entries), 1)
            self.assertGreater(state_entries[0]["offset"], 0)
            self.assertEqual(state_entries[0]["turn_count"], 1)
            self.assertEqual(state_entries[0]["partial_turn_ids"], [])

    def test_stop_flush_wait_commits_when_task_complete_arrives_late(self):
        # Codex Stop can fire a few hundred ms before task_complete is flushed
        # to the rollout. Without a bounded re-read, the turn uploads as
        # partial (207 ok) and never commits if no later Stop fires.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_dir = root / "state"
            turn_id = "turn-late-complete"
            rollout = root / f"rollout-{THREAD_ID}.jsonl"
            prefix = [
                {
                    "timestamp": "2026-07-12T00:00:00Z",
                    "type": "event_msg",
                    "payload": {"type": "task_started", "turn_id": turn_id},
                },
                {
                    "timestamp": "2026-07-12T00:00:01Z",
                    "type": "event_msg",
                    "payload": {"type": "user_message", "message": "hello"},
                },
                {
                    "timestamp": "2026-07-12T00:00:02Z",
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "working"}],
                        "internal_chat_message_metadata_passthrough": {
                            "turn_id": turn_id
                        },
                    },
                },
            ]
            complete = {
                "timestamp": "2026-07-12T00:00:03Z",
                "type": "event_msg",
                "payload": {
                    "type": "task_complete",
                    "turn_id": turn_id,
                    "last_agent_message": "done",
                },
            }
            rollout.write_text(
                "".join(json.dumps(row) + "\n" for row in prefix), encoding="utf-8"
            )

            class Response:
                def __enter__(self):
                    return self

                def __exit__(self, *_):
                    return False

                def getcode(self):
                    return 207

                def read(self):
                    return b'{"errors":[]}'

            appended = {"done": False}

            def sleepy_append(delay):
                # First flush sleep: append task_complete so the re-read sees it.
                if not appended["done"]:
                    with rollout.open("a", encoding="utf-8") as handle:
                        handle.write(json.dumps(complete) + "\n")
                    appended["done"] = True

            with (
                mock.patch.object(hook, "STATE_DIR", state_dir),
                mock.patch.object(hook, "STATE_FILE", state_dir / "state.json"),
                mock.patch.object(hook, "LOCK_FILE", state_dir / "state.lock"),
                mock.patch.object(
                    hook,
                    "resolve_config",
                    return_value=("https://example.test", "oc_test", None),
                ),
                mock.patch.object(
                    hook.urllib.request, "urlopen", return_value=Response()
                ),
                mock.patch.object(hook.time, "sleep", side_effect=sleepy_append),
                mock.patch.object(
                    sys,
                    "stdin",
                    io.StringIO(
                        json.dumps(
                            {
                                "session_id": THREAD_ID,
                                "transcript_path": str(rollout),
                                "hook_event_name": "Stop",
                            }
                        )
                    ),
                ),
            ):
                result = hook.main(["one_signal_codex_hook.py"])

            self.assertEqual(result, 0)
            self.assertTrue(appended["done"])
            state_entries = [
                value
                for key, value in json.loads(
                    (state_dir / "state.json").read_text(encoding="utf-8")
                ).items()
                if key != "_thread_paths"
            ]
            self.assertEqual(state_entries[0]["turn_count"], 1)
            self.assertGreater(state_entries[0]["offset"], 0)
            self.assertEqual(state_entries[0]["partial_turn_ids"], [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
