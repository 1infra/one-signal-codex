# One Signal for Codex CLI

Trace every Codex CLI turn — model generations, tool calls, and token
usage/cost — to your **One Infra** organization using a One Connector
access token. This is the Codex CLI counterpart of
[`one-signal-claude-code`](https://github.com/1infra/one-signal-claude-code)
(the Claude Code plugin): same
destination, same wire format, same One Connector access-token transport —
different source, because Codex's session data model is unrelated to
Claude Code's transcript format.

Install it the same way you install our Claude Code plugin: through the
tool's own plugin marketplace, no repo checkout required.

## What it does

After each Codex turn, Codex fires the plugin's `Stop` hook, which reads the
session's rollout transcript and uploads it to your One Infra org (via One
Connector) as a Langfuse trace: one root span per turn (`Codex CLI - Turn
N`), one generation per model round-trip (with token usage/cost), tool-call
spans nested under their generation, and reasoning captured as a lightweight
event (never the encrypted content blob — only its optional plaintext
summary). Traces from Codex CLI and Claude Code render in the same **Console
→ Observe** UI with the same shape.

## Install

```bash
codex plugin marketplace add 1infra/one-signal-codex
codex plugin add one-signal-codex@one-infra
mkdir -p ~/.codex && printf '{"ONE_SIGNAL_API_TOKEN":"oc_xxx"}' > ~/.codex/one-signal.json && chmod 600 ~/.codex/one-signal.json
```

Replace `oc_xxx` with your One Connector access token. That's it — start a
new `codex` session (or run `codex exec "..."`) and your turns show up in
**Console → Observe**.

- **Line 1** registers this repo as a Codex plugin marketplace (Codex reads
  `.agents/plugins/marketplace.json` at the repo root).
- **Line 2** installs and enables the plugin. It writes
  `[plugins."one-signal-codex@one-infra"] enabled = true` to
  `~/.codex/config.toml` for you. The plugin's Stop hook runs under Codex's
  **stable `hooks` feature** — on Codex 0.144.1 no extra feature flag is
  needed.
- **Line 3** stores your token where the hook reads it (chmod 600). You can
  use an environment variable instead — see [Configure](#configure).

### Getting a token

Console → **Access tokens** → create a new token (`oc_...`), named something
you'll recognize (e.g. "laptop — Codex CLI"). The token-created dialog and
the **Observe** onboarding card both show this exact install block with your
token pre-filled.

### Upgrading / uninstalling

```bash
codex plugin marketplace upgrade one-infra        # refresh the snapshot
codex plugin remove one-signal-codex@one-infra    # remove the plugin
```

## Configure

The hook resolves config at run time in this order: **environment variables
first, then `~/.codex/one-signal.json`.**

| Setting | Env var | JSON key | Description |
| --- | --- | --- | --- |
| API token | `ONE_SIGNAL_API_TOKEN` | `ONE_SIGNAL_API_TOKEN` | Your One Connector access token (`oc_...`). Required. |
| Base URL | `ONE_SIGNAL_BASE_URL` | `ONE_SIGNAL_BASE_URL` | Your One Connector deployment. Default `https://connector.1infra.io`. The hook POSTs to `<this>/api/public/otel/v1/traces`. |
| User ID | `ONE_SIGNAL_USER_ID` | `ONE_SIGNAL_USER_ID` | Optional self-reported metadata emitted as `one.signal.configured_user_id`. It is not an authentication identity; One Infra derives the authenticated actor from the access token. |
| Debug logging | `ONE_SIGNAL_CODEX_DEBUG=1` | — | Verbose logging to `~/.codex/one-signal-hook.log`. |
| Truncation | `ONE_SIGNAL_CODEX_MAX_CHARS` | — | Truncate captured inputs/outputs to this many characters. Default `20000`. |
| Instruction documents | `ONE_SIGNAL_CODEX_INSTRUCTION_DOCUMENTS` | — | Upload active global and project `AGENTS.md` snapshots for Intelligence compliance analysis. Default `true`. |

Env vars override the file, so you can keep the token in the file and
override the base URL per shell (or vice versa). `CODEX_HOME` is respected
everywhere — state, log, and config file all move under it if you've set it.

Environment-variable alternative to the JSON file:

```bash
export ONE_SIGNAL_API_TOKEN="oc_xxx"   # add to ~/.zshrc / ~/.bashrc to persist
```

## Requirements

- **Codex CLI ≥ 0.144** — this includes the `codex plugin add` command used
  above and enables hooks by default. Check yours with
  `codex --version` and `codex features list` (look for `hooks … stable …
  true`).
- **Python 3.10+** as `python3` on your `PATH` (the Stop hook is a `python3`
  command). No third-party packages — pure standard library.

## How it works

The plugin's `Stop` hook (`hooks/hooks.json`) runs
`python3 <plugin cache>/one_signal_codex_hook.py` after every turn. Codex
pipes a JSON payload to the hook's **stdin** carrying `session_id` and
`transcript_path` (the rollout file). The hook parses that rollout JSONL
incrementally (tracking a byte-offset cursor so re-runs only process new
turns) and builds one trace per completed turn: a root span
("Turn N"), one generation per model round-trip (with token usage when
available), tool-call spans nested under their generation, and reasoning
captured as a lightweight event. The internal events are converted to
OTLP/JSON and POSTed to
`<ONE_SIGNAL_BASE_URL>/api/public/otel/v1/traces` with HTTP Basic
authentication using the access token as the username and an empty
password. Requests contain at most 200 spans and 3.5 MB of final serialized
JSON.

Each turn is also attributed, matching the Claude Code plugin's tags so both
sources filter alike in Console → Observe: skills invoked in the turn
(detected from Codex's injected `<skill><name>…</name>` preamble) become
`skill:<name>` trace tags plus a `skill_names` metadata list, and MCP tool
calls become `mcp:<server>:<tool>` trace tags with `mcp_server` / `mcp_tool`
recorded on that tool's span metadata.

The hook attaches the active `~/.codex/AGENTS.md` and project-level
`AGENTS.md` snapshots so Intelligence can evaluate instruction compliance.
Nested snapshots are added when recorded tool activity first touches their
directory scope. Logical `AGENTS.md` paths are preserved when the file is a
symlink, while the uploaded content and hash come from its safe resolved
target. Image blocks remain excluded and each trace records the omitted image
count. Collection is capped at 20 files, 64,000 characters per file, and
256,000 characters total. Set `ONE_SIGNAL_CODEX_INSTRUCTION_DOCUMENTS=false`
to disable it.

The same hook script also accepts the legacy `notify` argv payload (thread
id, no transcript path — it globs `sessions/**/*-<thread-id>.jsonl`), and
falls back to the newest rollout on disk if a payload variant carries
neither. State (byte offset + turn count per session) lives under
`${CODEX_HOME:-~/.codex}/one-signal-state/state.json`.

## Reliability notes

- **Incremental checkpoint:** the byte-offset cursor only advances past
  turns whose spans were fully accepted upstream (every HTTP request
  returned 2xx) *and* that were completely parsed (a turn
  with no `task_complete` seen yet is left for the next run). Deterministic
  trace/observation IDs (`<thread_id>-t<turn_number>`) make retries
  idempotent upserts, not duplicates.
- **Stop-hook timing:** Codex fires `Stop` before appending `task_complete`.
  Like Langfuse's plugin, this hook uploads the in-progress trace immediately
  without advancing the checkpoint; a later Stop updates the same deterministic
  observation IDs with the completed timestamps and output. Interrupted turns
  are finalized with `aborted: true`.
- **Transient delivery failures:** network errors, HTTP 429, and HTTP 5xx are
  retried up to three times inside the same Hook run. The checkpoint advances
  only after every event for a completed turn is accepted upstream.
- `fcntl`-based locking doesn't exist on Windows; the hook proceeds without
  cross-process locking there (best-effort only).

## Privacy

This sends your Codex CLI turn data (prompts, assistant output, tool calls,
token usage) to `ONE_SIGNAL_BASE_URL`, authenticated with your One Connector
access token. Reasoning items' encrypted content blob (`encrypted_content`) is
never read or transmitted — only the optional plaintext summary (present only
if your Codex reasoning-summary setting is enabled) is captured, truncated
via `ONE_SIGNAL_CODEX_MAX_CHARS`.

## Troubleshooting

- **Nothing in Console → Observe:** set `ONE_SIGNAL_CODEX_DEBUG=1`, run a
  Codex turn, then check `~/.codex/one-signal-hook.log`. A line like
  `ingest failed: HTTP 401` means the hook is wired correctly but the token
  is wrong/inactive; `Processed N/M turns` with no error means it uploaded.
- **Hook not firing at all:** confirm `codex features list` shows `hooks …
  true`, and that `~/.codex/config.toml` has
  `[plugins."one-signal-codex@one-infra"] enabled = true` (added by
  `codex plugin add`). Newly installed hooks may need to be trusted the first
  time Codex runs them.
- **`403 token_inactive` / `401`:** the token in `~/.codex/one-signal.json`
  (or `ONE_SIGNAL_API_TOKEN`) is invalid or inactive — regenerate it in
  Console → Access tokens.
- **Other HTTP 4xx responses:** the request remains queued for a later hook
  run; check the debug log for the permanent rejection status.

## Self-test

```bash
uv run python test_hook.py -v
# or
uv run python one_signal_codex_hook.py --self-test
```

Runs the parser/assembly pipeline against a bundled fixture rollout
(`fixtures/sample_rollout.jsonl`) with no network calls, asserting the built
batch's event types, tool-span metadata, usage details, and that no
encrypted reasoning content ever leaks into an event.

## Release checklist

**Every version bump must run the release smoke before announcing.**

Unit tests alone are not enough: run a live `codex exec` path that hits a
failing tool call and fake secrets, then verify the uploaded trace on the
Console API (observation tree, `TOOL` + `level=ERROR`, no secret leak,
nonzero cost).

```bash
./scripts/release-smoke.sh
# optional:
CONNECTOR_URL=https://connector.1infra.io ./scripts/release-smoke.sh
```

The script runs the Codex session automatically and prints a verification
checklist + `curl`/`jq` template. Console Observe reads are
browser-session authenticated (not the plugin `oc_` token) — see
[scripts/release-smoke.md](scripts/release-smoke.md) for the full
procedure and gates.
