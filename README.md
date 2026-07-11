# One Signal for Codex CLI

Trace every Codex CLI turn — model generations, tool calls, and token
usage/cost — to your **One Infra** organization, with zero Langfuse
credentials on your machine. This is the Codex CLI counterpart of
[`plugins/one-signal`](../one-signal) (the Claude Code plugin): same
destination, same wire format, same One Connector access-token transport —
different source, because Codex's session data model is unrelated to
Claude Code's transcript format.

## Install

```bash
python3 plugins/one-signal-codex/install.py --token oc_xxx
```

This writes `~/.codex/one-signal.json` (chmod 600) and adds a `notify`
entry to `~/.codex/config.toml` pointing at the hook script. Restart any
running `codex` session (or just start a new one) to pick it up.

| Flag | Description |
| --- | --- |
| `--token` | Your One Connector access token (`oc_...`). Required. |
| `--base-url` | Your One Connector deployment URL. Default `https://connector.1infra.io`. The hook POSTs to `<this>/api/v1/observe/ingest`. |
| `--user-id` | Optional. User identifier attached to every trace. |
| `--uninstall` | Removes the `notify` wiring from `config.toml`. Leaves `one-signal.json` in place (delete it manually if you also want the token gone). |

If `config.toml` already has a **different** `notify` command configured,
the installer refuses to overwrite it (Codex only runs one notify command)
and prints instructions for chaining both via a small wrapper script.

### Getting a token

Same as the Claude Code plugin: Console → **Access tokens** → create a new
token (`oc_...`), named something you'll recognize (e.g. "laptop — Codex
CLI").

## Config reference

Resolved in this order at hook-run time: environment variables first, then
`~/.codex/one-signal.json`.

| Setting | Env var | Description |
| --- | --- | --- |
| Base URL | `ONE_SIGNAL_BASE_URL` | Default `https://connector.1infra.io`. |
| API token | `ONE_SIGNAL_API_TOKEN` | Your One Connector access token. |
| User ID | `ONE_SIGNAL_USER_ID` | Optional, attached to every trace. |
| Debug logging | `ONE_SIGNAL_CODEX_DEBUG=1` | Verbose logging to `~/.codex/one-signal-hook.log`. |
| Truncation | `ONE_SIGNAL_CODEX_MAX_CHARS` | Truncate captured inputs/outputs to this many characters. Default `20000`. |

`CODEX_HOME` is respected everywhere (state, log, and config file all move
under it if you've set it).

## Requirements

Python 3.10+ as `python3`. No third-party packages — pure standard
library, same as the Claude Code plugin.

## How it works

Codex CLI's `notify` config spawns a program **once per completed agent
turn**, passing it a single JSON argv (`{"type": "agent-turn-complete",
"thread-id": ..., "turn-id": ..., "last-assistant-message": ...}` — see the
top of `one_signal_codex_hook.py` for the exact verified shape). This
payload is a thin trigger; it has no rollout path and no token-usage/tool
detail. The hook uses it to resolve `thread-id` to a session rollout file
under `~/.codex/sessions/YYYY/MM/DD/rollout-<ts>-<thread-id>.jsonl`, then
parses that file incrementally (tracking a byte-offset cursor so re-runs
only process new turns) and builds one Langfuse trace per completed turn:
a root span ("Turn N"), one generation per model round-trip (with token
usage when available), tool-call spans nested under their generation, and
reasoning captured as a lightweight event (never the encrypted content
blob, only its optional plaintext summary). The batch is POSTed to
`<ONE_SIGNAL_BASE_URL>/api/v1/observe/ingest`, identically to the Claude
Code plugin — traces from both tools render in the same Traces UI with the
same shape.

State (byte offset + turn count per session, plus a thread-id → rollout-path
cache) lives in `~/.codex/one-signal-state/state.json`, mirroring
`~/.claude/state/one_signal_state.json` for the Claude plugin.

## Known limitation: TUI vs. headless `codex exec`

The commonly stated assumption is that `notify` only fires in the
interactive TUI and never in headless `codex exec`. **On the Codex CLI
version this was built and verified against (`codex-cli 0.144.1`), that
turned out not to be true**: `codex exec` fired `notify` identically to the
TUI (with `client: "codex_exec"` instead of `"codex-tui"` in the payload),
confirmed with a live, non-interactive smoke test, and the Rust call site
(`run_legacy_after_agent_hook` in `codex-rs/core/src/session/turn.rs`) is
unconditional in the turn loop shared by both surfaces.

This may differ on other CLI versions/builds — if your `codex exec` runs
aren't showing up in Console → Observe, set `ONE_SIGNAL_CODEX_DEBUG=1` and
check `~/.codex/one-signal-hook.log` for whether the hook was invoked at
all versus invoked-but-failed.

What genuinely does **not** reach this hook: any Codex surface that never
fires `notify` — e.g. `codex mcp-server` / `app-server` embedding modes
driven by an external harness that bypasses the CLI's own notify wiring,
or another tool already occupying the single `notify` slot (see the
installer's refusal behavior above).

## Reliability notes

- Same incremental-checkpoint discipline as the Claude plugin: the
  byte-offset cursor only advances past turns whose events were fully
  accepted upstream (every HTTP chunk 2xx, and any 207 response's
  per-event `errors` empty) *and* that were completely parsed (a turn with
  no `task_complete` seen yet is left for the next run). Deterministic
  trace/observation IDs (`<thread_id>-t<turn_number>`) make retries
  idempotent upserts, not duplicates.
- The state-file lock (`~/.codex/one-signal-state/state.lock`) is a single
  global lock shared by every Codex session on the machine, same
  known-deferred trade-off as the Claude plugin.
- `fcntl`-based locking doesn't exist on Windows; the hook proceeds without
  cross-process locking there (best-effort only).
- Rollout-file discovery globs `~/.codex/sessions/**/*-<thread-id>.jsonl`
  the first time a given session is seen, then caches the resolved path —
  subsequent turns of a long session are O(1), not a repeated filesystem
  walk.
- If your organization hasn't connected Langfuse yet, the proxy responds
  `503 signal_not_configured`; the hook logs a hint to the debug log and
  exits cleanly without advancing the checkpoint, so it retries
  automatically once Langfuse is connected.

## Privacy

Same data-handling posture as the Claude Code plugin: this sends your
Codex CLI turn data (prompts, assistant output, tool calls, token usage) to
`ONE_SIGNAL_BASE_URL`, authenticated with your One Connector access token,
which forwards it into your organization's own Langfuse project. Reasoning
items' encrypted content blob (`encrypted_content`) is never read or
transmitted — only the optional plaintext summary (present only if your
Codex reasoning-summary setting is enabled) is captured, truncated the same
way as everything else.

## Troubleshooting

- Nothing showing up in Console → Observe: set `ONE_SIGNAL_CODEX_DEBUG=1`,
  run a Codex turn, then check `~/.codex/one-signal-hook.log`.
- `503 signal_not_configured` in the log: your organization hasn't
  connected Langfuse yet — do that in Console → Integrations.
- Hook not firing at all: check `notify` in `~/.codex/config.toml` points
  at `one_signal_codex_hook.py`; re-run `install.py` if not.
- `codex features list` can confirm which Codex features (e.g.
  `unified_exec`) are active in your build — this affects tool names
  (`exec_command` vs. `shell`) but not the hook's parsing, which handles
  both `function_call`/`function_call_output` and
  `custom_tool_call`/`custom_tool_call_output` shapes generically.

## Self-test

```bash
python3 plugins/one-signal-codex/test_hook.py -v
# or
python3 plugins/one-signal-codex/one_signal_codex_hook.py --self-test
```

Runs the parser/assembly pipeline against a bundled fixture rollout
(`fixtures/sample_rollout.jsonl`) with no network calls, asserting the
built batch's event types, tool-span metadata, usage details, and that no
encrypted reasoning content ever leaks into an event.
