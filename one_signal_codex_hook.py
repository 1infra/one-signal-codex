#!/usr/bin/env python3
"""
Codex CLI -> One Signal (via One Connector) hook.

Sibling of ../one-signal (the Claude Code plugin). Same destination, same
Langfuse *classic ingestion* wire format, same One Connector access-token
transport -- this file is a fresh implementation because Codex's session
data model is unrelated to Claude Code's transcript format (see below), not
a derivative of the Claude hook's parsing code.

--------------------------------------------------------------------------
How Codex invokes this
--------------------------------------------------------------------------
Two transports feed this same hook; it auto-detects which one fired.

(A) Plugin `Stop` hook -- the PRIMARY, marketplace path (see
README.md and .codex-plugin/plugin.json +
hooks/hooks.json). When the plugin is installed via `codex plugin
marketplace add 1infra/one-signal-codex` + `codex plugin add one-signal-codex@one-infra`,
Codex runs the plugin's declared Stop `command` after every turn and pipes a
JSON object to STDIN (verified against codex-cli 0.144.1):

    {
      "session_id": "<uuid, == the rollout filename's trailing UUID>",
      "turn_id": "<uuid>",
      "transcript_path": "<abs path to the rollout jsonl>",
      "cwd": "<turn cwd>",
      "model": "<model slug>",
      "last_assistant_message": "<final assistant text>" | null,
      "hook_event_name": "Stop",
      ...
    }

Unlike notify, this DOES carry `transcript_path` directly, so the rollout is
used as-is (no glob). This path fires under Codex's now-stable `hooks`
feature -- the older `plugin_hooks` feature flag is REMOVED on 0.144.1 and
is not required (verified: the Stop hook fires with `plugin_hooks=false`).

(B) Legacy `notify` config (~/.codex/config.toml: `notify = ["python3",
"<abs path to this file>"]`, written by install.py) -- the manual/alternative
path -- spawns this script ONCE per completed agent
turn and appends exactly one argv: a JSON payload. Verified against
installed `codex-cli 0.144.1` and against the source
(codex-rs/hooks/src/legacy_notify.rs, `UserNotification::AgentTurnComplete`,
`#[serde(rename_all = "kebab-case")]`):

    {
      "type": "agent-turn-complete",
      "thread-id": "<uuid, matches the rollout filename's trailing UUID>",
      "turn-id": "<uuid>",
      "cwd": "<turn cwd>",
      "client": "codex-tui" | "codex_exec" | ...,   # omitted if unknown
      "input-messages": ["<user text of this turn>", ...],
      "last-assistant-message": "<final assistant text>" | null
    }

The payload carries NO rollout file path and NO token usage / tool-call
detail -- it is a thin trigger, not the data source. This hook uses it only
to (a) confirm it was actually invoked for a turn and (b) resolve
`thread-id` to a rollout file; all trace content is parsed from the rollout
JSONL itself.

KNOWN LIMIT, one correction vs. the commonly repeated assumption: notify
does NOT appear to be TUI-only. On the installed build above, `codex exec`
(headless, non-interactive) fired notify identically (`client:
"codex_exec"`), confirmed with a live `codex exec` smoke test, and the
Rust call site (`codex-rs/core/src/session/turn.rs` ->
`run_legacy_after_agent_hook`) is unconditional in the shared turn loop
used by both TUI and exec. This may differ on other CLI versions/builds; if
your `codex exec` runs are not showing up in Console -> Observe, that is
the first thing to check with ONE_SIGNAL_CODEX_DEBUG=1. What genuinely does
NOT reach this hook: any Codex surface that never fires `notify` at all --
e.g. the `codex mcp-server` / `app-server` embedding modes driven by an
external harness that doesn't run the CLI's own notify wiring, or a
`notify` entry pointed elsewhere by another tool (see install.py's refusal
behavior for that case).

--------------------------------------------------------------------------
Where the data comes from: the rollout JSONL
--------------------------------------------------------------------------
Full conversation detail lives under
`${CODEX_HOME:-~/.codex}/sessions/YYYY/MM/DD/rollout-<ts>-<thread-id>.jsonl`
(verified: the trailing UUID in the filename equals both the notify
payload's `thread-id` and the file's own first line,
`session_meta.payload.session_id` / `.id`). Each line is
`{"timestamp", "type", "payload"}`. The `type`s this hook understands
(verified against real rollout files produced by this Codex build):

  - `session_meta`   (payload.session_id / payload.id == thread-id)
  - `turn_context`   (payload.turn_id, payload.model -- per-turn model/cwd)
  - `event_msg` / `task_started`   (payload.turn_id -- opens a turn)
  - `event_msg` / `task_complete`  (payload.turn_id, payload.last_agent_message
                                     -- closes a turn)
  - `event_msg` / `token_count`    (payload.info.last_token_usage -- the
                                     incremental usage for the model call
                                     that just returned; no turn_id field,
                                     attributed positionally)
  - `event_msg` / `mcp_tool_call_end`  (payload.invocation.server/tool,
                                     payload.result, payload.duration -- an
                                     MCP tool call's completion; no turn_id
                                     field, attributed positionally to the
                                     most recently opened turn. Pairs with a
                                     preceding function_call by call_id when
                                     present, otherwise emitted as its own
                                     attributed span)
  - `response_item` / `message`    (role user/assistant/developer; content
                                     blocks type `input_text`/`output_text`;
                                     carries
                                     `internal_chat_message_metadata_passthrough
                                     .turn_id` on real conversation turns --
                                     absent on injected system/developer
                                     preamble, which this hook treats as
                                     noise and skips, mirroring how the
                                     Claude hook skips `isMeta` rows. A
                                     user-role message whose text BEGINS with
                                     the `<skill><name>...` preamble is a
                                     skill injection and is recorded as
                                     turn attribution -- see _SKILL_MARKER)
  - `response_item` / `reasoning`  (payload.summary: list[str], usually
                                     empty unless reasoning summaries are
                                     enabled; payload.encrypted_content is
                                     opaque ciphertext and is NEVER sent
                                     upstream)
  - `response_item` / `function_call` + `function_call_output`
                                    (payload.name/arguments/call_id +
                                     payload.call_id/output -- paired by
                                     call_id)
  - `response_item` / `custom_tool_call` + `custom_tool_call_output`
                                    (same shape, different field names:
                                     payload.input instead of .arguments;
                                     used when e.g. the `unified_exec`
                                     feature is on -- treated identically to
                                     function_call/_output)

--------------------------------------------------------------------------
Turn -> Langfuse event assembly
--------------------------------------------------------------------------
One trace per turn (task_started..task_complete), mirroring the Claude
hook's "one trace per Stop-hook turn":
  - trace-create + root SPAN observation ("Turn N")
  - one GENERATION per model round-trip inside the turn. A round starts at
    each `reasoning` response_item (Codex emits one per model call) and
    ends at the next one / turn end; this naturally buckets that round's
    assistant message(s) and tool call(s) together. If a turn has no
    `reasoning` items at all, the whole turn becomes a single generation
    (fallback for schema variance).
  - one SPAN per tool call, nested under its generation, name
    "Tool: <name>", metadata.tool_name/tool_id set -- SAME encoding as the
    Claude hook (classic ingestion's observation-create only accepts
    GENERATION | SPAN | EVENT; there is no "TOOL" type).
  - one EVENT per reasoning item, nested under its generation, carrying
    only `summary` (if non-empty) + a boolean flag -- never
    `encrypted_content`.
  - usageDetails on the generation from the next `token_count` event
    encountered after that round's items (positional attribution, since
    token_count carries no turn_id/round marker of its own).

--------------------------------------------------------------------------
Transport, state, and config
--------------------------------------------------------------------------
Same ingest endpoint, same batch envelope, same chunking/retry/checkpoint
discipline as ../one-signal/hooks/one_signal_hook.py (see that file's
module docstring for the FIX A / FIX B commit-past-accepted-only rationale
-- reused verbatim here, just keyed by thread_id/rollout_path instead of
session_id/transcript_path). State lives under
`${CODEX_HOME:-~/.codex}/one-signal-state/`. Config resolves from env vars
first (ONE_SIGNAL_API_TOKEN, ONE_SIGNAL_BASE_URL, ONE_SIGNAL_USER_ID) then
`${CODEX_HOME:-~/.codex}/one-signal.json` (chmod 600). The token is never
printed or logged. All failures are swallowed and this always exits 0 --
notify is fire-and-forget from Codex's side (`Command::spawn`, not
awaited), but exiting 0 defensively means a bug here can never surface as a
Codex-visible error either way.
"""

import hashlib
import json
import logging
import os
import re
import sys
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

PLUGIN_VERSION = "0.1.1"

# --- Paths ---
def _codex_home() -> Path:
    override = os.environ.get("CODEX_HOME")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".codex"

CODEX_HOME = _codex_home()
SESSIONS_DIR = CODEX_HOME / "sessions"
STATE_DIR = CODEX_HOME / "one-signal-state"
LOG_FILE = CODEX_HOME / "one-signal-hook.log"
STATE_FILE = STATE_DIR / "state.json"
LOCK_FILE = STATE_DIR / "state.lock"
CONFIG_FILE = CODEX_HOME / "one-signal.json"

def _env_truthy(name: str) -> bool:
    return (os.environ.get(name) or "").strip().lower() in ("1", "true", "yes")

DEBUG = _env_truthy("ONE_SIGNAL_CODEX_DEBUG")
try:
    MAX_CHARS = int(os.environ.get("ONE_SIGNAL_CODEX_MAX_CHARS") or "20000")
except ValueError:
    MAX_CHARS = 20000

# Server-side caps for the ingest proxy (same proxy, same caps as the Claude
# hook -- mirrors Langfuse's own /api/public/ingestion batch-size limits).
MAX_EVENTS_PER_BATCH = 200
MAX_BYTES_PER_BATCH = 3_500_000
ROLLOUT_FLUSH_DELAYS = (0.05, 0.1, 0.2, 0.4, 0.8)

# ----------------- Logging -----------------
_logger: Optional[logging.Logger] = None

def _get_logger() -> Optional[logging.Logger]:
    global _logger
    if _logger is not None:
        return _logger
    try:
        CODEX_HOME.mkdir(parents=True, exist_ok=True)
        lg = logging.getLogger("one_signal_codex_hook")
        lg.setLevel(logging.DEBUG if DEBUG else logging.INFO)
        if not lg.handlers:
            h = RotatingFileHandler(str(LOG_FILE), maxBytes=5_000_000, backupCount=3)
            h.setFormatter(logging.Formatter(
                "%(asctime)s [%(levelname)s] %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            ))
            lg.addHandler(h)
        _logger = lg
        return _logger
    except Exception:
        return None

def debug(msg: str) -> None:
    if not DEBUG:
        return
    lg = _get_logger()
    if lg is not None:
        try:
            lg.debug(msg)
        except Exception:
            pass

def info(msg: str) -> None:
    # Only ever written when DEBUG is on -- notify fires on every turn of
    # every session on the machine, so an always-on info log would grow
    # unbounded for a hook nobody is actively debugging.
    if not DEBUG:
        return
    lg = _get_logger()
    if lg is not None:
        try:
            lg.info(msg)
        except Exception:
            pass

def warning(msg: str) -> None:
    if not DEBUG:
        return
    lg = _get_logger()
    if lg is not None:
        try:
            lg.warning(msg)
        except Exception:
            pass

# ----------------- State locking (best-effort; see ../one-signal for the
# fcntl / Windows-fallback rationale, reused verbatim) -----------------
class FileLock:
    def __init__(self, path: Path, timeout_s: float = 2.0):
        self.path = path
        self.timeout_s = timeout_s
        self._fh = None

    def __enter__(self):
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.path, "a+", encoding="utf-8")
        self.acquired = False
        try:
            import fcntl  # Unix only
        except ImportError:
            return self
        deadline = time.time() + self.timeout_s
        try:
            while True:
                try:
                    fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    self.acquired = True
                    return self
                except BlockingIOError:
                    if time.time() > deadline:
                        raise TimeoutError(
                            f"could not acquire {self.path} within {self.timeout_s}s"
                        )
                    time.sleep(0.05)
        except BaseException:
            try:
                self._fh.close()
            except Exception:
                pass
            raise

    def __exit__(self, exc_type, exc, tb):
        try:
            import fcntl
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        try:
            self._fh.close()
        except Exception:
            pass

def load_state() -> Dict[str, Any]:
    try:
        if not STATE_FILE.exists():
            return {}
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}

def save_state(state: Dict[str, Any]) -> None:
    tmp: Optional[Path] = None
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(days=30)
        for k in list(state.keys()):
            entry = state.get(k)
            if not isinstance(entry, dict):
                continue
            updated = entry.get("updated")
            if not isinstance(updated, str):
                continue
            try:
                ts = datetime.fromisoformat(updated.replace("Z", "+00:00"))
            except Exception:
                continue
            if ts < cutoff:
                del state[k]
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = STATE_DIR / f"{STATE_FILE.name}.tmp.{os.getpid()}.{uuid.uuid4().hex[:8]}"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(json.dumps(state, indent=2, sort_keys=True))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, STATE_FILE)
        tmp = None
    except Exception as e:
        debug(f"save_state failed: {e}")
        if tmp is not None:
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass

def state_key(thread_id: str, rollout_path: str) -> str:
    raw = f"{thread_id}::{rollout_path}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()

# ----------------- Notify payload -----------------
def read_notify_payload(argv: List[str]) -> Dict[str, Any]:
    """Codex passes exactly one JSON argv after the configured notify
    command/args. Tolerates a missing/unparseable argv by returning {}."""
    if len(argv) < 2:
        debug("no notify payload argv present")
        return {}
    try:
        parsed = json.loads(argv[1])
        if isinstance(parsed, dict):
            debug(f"notify payload keys: {sorted(parsed.keys())}")
            return parsed
    except Exception as e:
        debug(f"read_notify_payload exception: {e!r}")
    return {}

def extract_thread_id(payload: Dict[str, Any]) -> Optional[str]:
    # "thread-id" is the real (kebab-case, serde-renamed) field; tolerate a
    # couple of plausible alternate spellings in case of future/older
    # builds, same defensive spirit as the Claude hook's field probing.
    return (
        payload.get("thread-id")
        or payload.get("thread_id")
        or payload.get("session_id")
    )

# ----------------- Plugin `Stop` hook payload (stdin) -----------------
def read_stop_payload() -> Dict[str, Any]:
    """Codex's plugin `Stop` hook (config.toml `[features] plugin_hooks =
    true` + an enabled plugin whose hooks.json declares a Stop `command`)
    pipes a JSON object to stdin instead of an argv:

        {"session_id": "<uuid>", "turn_id": "<uuid>",
         "transcript_path": "<abs path to the rollout jsonl>",
         "hook_event_name": "Stop"}

    (Shape verified against langfuse/codex-observability-plugin's HookInput
    type and codex-cli 0.144.1.) Unlike the legacy `notify` argv, this DOES
    carry the rollout path directly, so we don't need to glob for it.

    Best-effort: returns {} for an interactive tty, empty, or unparseable
    stdin so the legacy notify/argv path is never disturbed."""
    try:
        if sys.stdin is None or sys.stdin.isatty():
            return {}
        raw = sys.stdin.read()
    except Exception as e:
        debug(f"read_stop_payload read exception: {e!r}")
        return {}
    raw = (raw or "").strip()
    if not raw:
        debug("no stop-hook payload on stdin")
        return {}
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            debug(f"stop payload keys: {sorted(parsed.keys())}")
            return parsed
    except Exception as e:
        debug(f"read_stop_payload parse exception: {e!r}")
    return {}

# ----------------- Rollout file discovery -----------------
def thread_id_from_path(rollout_path: Path) -> Optional[str]:
    """Best-effort thread id for a rollout whose trigger payload didn't carry
    one: prefer the `session_meta` id on the first line, else the trailing
    UUID in the filename (`rollout-<ts>-<uuid>.jsonl`)."""
    try:
        with rollout_path.open("r", encoding="utf-8") as fh:
            obj = json.loads(fh.readline())
        payload = obj.get("payload", obj) if isinstance(obj, dict) else {}
        sid = payload.get("id") or payload.get("session_id")
        if sid:
            return str(sid)
    except Exception as e:
        debug(f"thread_id_from_path first-line read failed: {e!r}")
    m = re.search(r"[0-9a-fA-F-]{36}$", rollout_path.stem)
    return m.group(0) if m else None

def newest_rollout() -> Optional[Path]:
    """Last-resort discovery when neither a thread id nor a transcript_path is
    available: the most recently modified rollout file on disk. Lets the hook
    still capture the current session under payload variants that omit both."""
    if not SESSIONS_DIR.exists():
        debug(f"sessions dir does not exist: {SESSIONS_DIR}")
        return None
    candidates = list(SESSIONS_DIR.rglob("rollout-*.jsonl"))
    if not candidates:
        debug("no rollout files found for newest-rollout fallback")
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)

def resolve_rollout(
    thread_id: Optional[str],
    transcript_hint: Optional[str],
    state: Dict[str, Any],
) -> Optional[Path]:
    """Resolve the rollout file from whichever trigger fired, in priority:
      1. `transcript_path` handed to us directly by the plugin Stop hook.
      2. `thread-id` (notify argv or Stop payload) -> cached path or glob.
      3. newest rollout on disk (fallback).
    Caches a resolved (thread_id -> path) mapping into `state` when both
    are known, matching find_rollout_path's cache discipline."""
    if transcript_hint:
        p = Path(transcript_hint).expanduser()
        if p.exists():
            if thread_id:
                cache = state.setdefault("_thread_paths", {})
                cache[thread_id] = str(p)
            return p
        debug(f"transcript_path does not exist, falling through: {transcript_hint}")
    if thread_id:
        found = find_rollout_path(thread_id, state)
        if found is not None:
            return found
    return newest_rollout()


def find_rollout_path(thread_id: str, state: Dict[str, Any]) -> Optional[Path]:
    """Resolve thread_id -> rollout file path. The notify payload never
    carries the path (verified against codex-rs/hooks/src/legacy_notify.rs),
    so this either reuses a cached path from a prior turn of the same
    session (fast path -- O(1), no filesystem walk) or globs
    sessions/**/*-<thread_id>.jsonl once and caches the result."""
    cache = state.get("_thread_paths", {})
    cached = cache.get(thread_id)
    if cached and Path(cached).exists():
        return Path(cached)

    if not SESSIONS_DIR.exists():
        debug(f"sessions dir does not exist: {SESSIONS_DIR}")
        return None

    matches = sorted(SESSIONS_DIR.rglob(f"*{thread_id}.jsonl"))
    if not matches:
        debug(f"no rollout file found for thread_id={thread_id}")
        return None
    chosen = matches[-1]  # most recent path lexically if somehow >1

    cache[thread_id] = str(chosen)
    state["_thread_paths"] = cache
    return chosen

# ----------------- Content extraction helpers -----------------
def extract_text(content: Any) -> str:
    """Join text blocks from a response_item message's content array (or a
    reasoning item's `summary` array, which uses the same
    {"type": ..., "text": ...} block shape under type "summary_text" --
    verified against a real rollout where reasoning summaries were
    enabled). Non-text blocks (images, etc.) are skipped -- not captured in
    v1."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
                continue
            if not isinstance(block, dict):
                continue
            if block.get("type") in ("input_text", "output_text", "text", "summary_text"):
                parts.append(block.get("text", ""))
        return "\n".join(p for p in parts if p)
    return ""

def truncate_text(s: Optional[str], max_chars: int = MAX_CHARS) -> Tuple[str, Dict[str, Any]]:
    if not s:
        return "", {"truncated": False, "orig_len": 0}
    orig_len = len(s)
    if orig_len <= max_chars:
        return s, {"truncated": False, "orig_len": orig_len}
    head = s[:max_chars]
    return head, {
        "truncated": True,
        "orig_len": orig_len,
        "kept_len": len(head),
        "sha256": hashlib.sha256(s.encode("utf-8")).hexdigest(),
    }

def parse_timestamp(value: Any) -> Optional[datetime]:
    if isinstance(value, dict):
        value = value.get("timestamp")
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return None

def row_turn_id(payload: Dict[str, Any]) -> Optional[str]:
    """The turn_id a response_item belongs to, from
    internal_chat_message_metadata_passthrough.turn_id. Injected
    system/developer preamble carries no such metadata and is intentionally
    treated as not belonging to any turn (skipped), the same way the Claude
    hook skips isMeta rows."""
    meta = payload.get("internal_chat_message_metadata_passthrough")
    if isinstance(meta, dict):
        tid = meta.get("turn_id")
        if isinstance(tid, str) and tid:
            return tid
    return None

def tool_call_input(payload: Dict[str, Any]) -> Any:
    # function_call uses "arguments" (a JSON string); custom_tool_call uses
    # "input" (often a raw script string, e.g. exec_command's JS). Both are
    # opaque strings from this hook's point of view -- just truncate them.
    if "arguments" in payload:
        return payload.get("arguments")
    return payload.get("input")

def tool_call_output_text(payload: Dict[str, Any]) -> str:
    out = payload.get("output")
    if isinstance(out, str):
        return out
    if isinstance(out, list):
        # custom_tool_call_output's `output` is a list of {"type":
        # "input_text", "text": ...} blocks (same shape as message content).
        return extract_text(out)
    if out is None:
        return ""
    try:
        return json.dumps(out, ensure_ascii=False)
    except Exception:
        return str(out)

COMMAND_TOOL_NAMES = frozenset({"exec", "exec_command", "shell", "shell_command", "write_stdin"})

def tool_call_exit_code(payload: Dict[str, Any]) -> Optional[int]:
    """Return an exit code only from the tool wrapper's structured output."""
    out = payload.get("output")
    structured = out if isinstance(out, dict) else None
    if structured is None:
        try:
            parsed = json.loads(tool_call_output_text(payload))
            structured = parsed if isinstance(parsed, dict) else None
        except (json.JSONDecodeError, TypeError):
            structured = None
    if structured is not None:
        code = structured.get("exit_code")
        if (
            isinstance(code, int)
            and not isinstance(code, bool)
            and abs(code) <= 9_007_199_254_740_991
        ):
            return code
    return None

# ----------------- Incremental reader (byte-safe, mirrors the Claude
# hook's read_new_jsonl -- see that file's FIX C comment for why splitting
# on raw b"\n" before decoding is safe) -----------------
def read_new_lines(rollout_path: Path, start_offset: int) -> List[Tuple[Dict[str, Any], int]]:
    if not rollout_path.exists():
        return []

    offset = start_offset
    try:
        file_size = rollout_path.stat().st_size
        if file_size < offset:
            debug(f"rollout shrank ({file_size} < {offset}); restarting")
            offset = 0
        with open(rollout_path, "rb") as f:
            f.seek(offset)
            chunk = f.read()
    except Exception as e:
        debug(f"read_new_lines failed: {e}")
        return []

    if not chunk:
        return []

    lines = chunk.split(b"\n")
    complete_lines = lines[:-1]  # last element has no trailing "\n"

    rows: List[Tuple[Dict[str, Any], int]] = []
    pos = offset
    for line_bytes in complete_lines:
        pos += len(line_bytes) + 1
        stripped = line_bytes.strip()
        if not stripped:
            continue
        try:
            row = json.loads(stripped.decode("utf-8"))
        except Exception as e:
            debug(f"skipping unparsable rollout line: {type(e).__name__}: {e}")
            continue
        rows.append((row, pos))

    return rows


def read_completed_turns_after_flush(
    rollout_path: Path, start_offset: int
) -> Tuple[List[Tuple[Dict[str, Any], int]], List["Turn"]]:
    rows = read_new_lines(rollout_path, start_offset)
    if not rows:
        return rows, []

    # Codex can invoke Stop just before it appends task_complete. Re-read for
    # a short bounded window so a session's final turn uploads on this Stop
    # instead of waiting for another turn that may never happen.
    for delay in ROLLOUT_FLUSH_DELAYS:
        latest_started: Optional[str] = None
        completed: set[str] = set()
        for row, _ in rows:
            payload = row.get("payload")
            if row.get("type") != "event_msg" or not isinstance(payload, dict):
                continue
            if payload.get("type") == "task_started" and isinstance(payload.get("turn_id"), str):
                latest_started = payload["turn_id"]
            elif payload.get("type") == "task_complete" and isinstance(payload.get("turn_id"), str):
                completed.add(payload["turn_id"])
        if latest_started is None or latest_started in completed:
            break
        time.sleep(delay)
        rows = read_new_lines(rollout_path, start_offset) or rows

    return rows, build_turns(rows)

# ----------------- Turn assembly -----------------
@dataclass
class TurnRound:
    """One model round-trip inside a turn: bounded by consecutive
    `reasoning` items (or by the turn's own boundaries if the turn has no
    reasoning items)."""
    reasoning: Optional[Dict[str, Any]] = None
    reasoning_ts: Optional[datetime] = None
    assistant_texts: List[Tuple[str, Optional[datetime]]] = field(default_factory=list)
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)  # normalized dicts
    usage: Optional[Dict[str, Any]] = None  # event_msg/token_count.payload.info
    usage_ts: Optional[datetime] = None

@dataclass
class Turn:
    turn_id: str
    start_offset: int
    end_offset: int = 0
    user_text: str = ""
    user_text_authoritative: bool = False  # set once by event_msg/user_message
    user_ts: Optional[datetime] = None
    model: Optional[str] = None
    cwd: Optional[str] = None
    last_agent_message: Optional[str] = None
    completed_ts: Optional[datetime] = None
    duration_ms: Optional[int] = None
    rounds: List[TurnRound] = field(default_factory=list)
    skills: List[str] = field(default_factory=list)
    pending_skills: List[Tuple[str, str]] = field(default_factory=list)
    complete: bool = False

def _normalize_tool_call(payload: Dict[str, Any], ts: Optional[datetime]) -> Dict[str, Any]:
    return {
        "call_id": payload.get("call_id"),
        "name": payload.get("name") or "unknown",
        "input": tool_call_input(payload),
        "ts": ts,
        "output": None,
        "output_ts": None,
    }

# When a Codex turn invokes a skill, the CLI injects the skill's resolved
# content as an extra user-role response_item whose text BEGINS with the
# literal preamble `<skill>\n<name>NAME</name>...`. Anchored to the start of
# the (stripped) message on purpose: across 802/802 real skill injections in
# local rollouts every one starts with this marker, never mid-text, so
# anchoring is what distinguishes a genuine injection from a user prompt that
# merely quotes the markup ("Explain <skill>..."). Skill names are short
# slugs; the cap bounds an adversarial or malformed injection so it cannot
# mint an oversized trace tag (the server independently re-caps on read).
_SKILL_MARKER = re.compile(r"^\s*<skill>\s*<name>\s*([^<\s][^<]*?)\s*</name>", re.IGNORECASE)
MAX_SKILL_NAME_CHARS = 128
MAX_SKILLS_PER_TURN = 100

def _skill_name_from_text(text: str) -> Optional[str]:
    match = _SKILL_MARKER.match(text or "")
    if not match:
        return None
    return match.group(1).strip()[:MAX_SKILL_NAME_CHARS] or None

def _skill_name_from_payload(payload: Dict[str, Any]) -> Optional[str]:
    return _skill_name_from_text(extract_text(payload.get("content")))

def _record_skill(turn: Turn, skill: str) -> None:
    if skill not in turn.skills and len(turn.skills) < MAX_SKILLS_PER_TURN:
        turn.skills.append(skill)

def _mcp_result_text(result: Any) -> str:
    if isinstance(result, dict):
        for status in ("Ok", "Err"):
            value = result.get(status)
            if isinstance(value, dict) and "content" in value:
                text = extract_text(value["content"])
                if text:
                    return text
    return json.dumps(result, ensure_ascii=False) if result is not None else ""

def _mcp_result_status(result: Any) -> str:
    if isinstance(result, dict):
        if "Err" in result:
            return "error"
        if "Ok" in result:
            return "success"
    return "unknown"

def _mcp_start_ts(end_ts: Optional[datetime], duration: Any) -> Optional[datetime]:
    """Recover a span start time from mcp_tool_call_end's own end time and its
    reported `{"secs": ..., "nanos": ...}` duration, so a synthesized MCP span
    (no paired function_call to supply a start) reports real latency instead
    of collapsing to zero."""
    if end_ts is None or not isinstance(duration, dict):
        return end_ts
    secs = duration.get("secs")
    nanos = duration.get("nanos")
    if not isinstance(secs, (int, float)) or not isinstance(nanos, (int, float)):
        return end_ts
    return end_ts - timedelta(seconds=secs, microseconds=nanos / 1000)

def build_turns(rows: List[Tuple[Dict[str, Any], int]]) -> List[Turn]:
    """Groups incremental rollout rows into complete turns. A turn opens at
    event_msg/task_started and closes at the matching event_msg/task_complete
    (by turn_id). Anything left open at the end of `rows` (no task_complete
    seen yet in this read) is NOT returned -- its bytes stay uncommitted so
    the next hook invocation re-reads and completes it, same "never commit
    past incomplete data" discipline as the Claude hook."""
    turns: List[Turn] = []
    open_turns: Dict[str, Turn] = {}
    turn_context_by_id: Dict[str, Dict[str, Any]] = {}
    # tool calls awaiting their output, keyed by call_id, scoped per turn
    pending_tool_calls: Dict[str, Dict[str, Tuple[str, Dict[str, Any]]]] = {}
    # token_count events queued for positional attribution to the next
    # round that closes, per turn_id
    pending_usage: Dict[str, List[Tuple[Dict[str, Any], Optional[datetime]]]] = {}

    def current_round(turn: Turn) -> TurnRound:
        if not turn.rounds:
            turn.rounds.append(TurnRound())
        return turn.rounds[-1]

    def attach_usage(turn: Turn) -> None:
        queue = pending_usage.get(turn.turn_id) or []
        if queue and turn.rounds:
            usage_payload, usage_ts = queue.pop(0)
            turn.rounds[-1].usage = usage_payload
            turn.rounds[-1].usage_ts = usage_ts
            pending_usage[turn.turn_id] = queue

    # Turns without ANY task_started in this read range but with an open
    # turn_id we haven't seen the start of are out of scope here -- each
    # hook run only ever resumes at a byte offset that was previously
    # committed at a turn boundary, so a task_started for the next turn is
    # always present before its response_items in the same or an earlier
    # read.
    for row, row_offset in rows:
        row_type = row.get("type")
        payload = row.get("payload")
        if not isinstance(payload, dict):
            continue

        if row_type == "event_msg" and payload.get("type") == "task_started":
            tid = payload.get("turn_id")
            if not isinstance(tid, str) or not tid:
                continue
            open_turns[tid] = Turn(turn_id=tid, start_offset=row_offset)
            pending_tool_calls[tid] = {}
            pending_usage[tid] = []
            continue

        if row_type == "turn_context":
            tid = payload.get("turn_id")
            if isinstance(tid, str) and tid:
                turn_context_by_id[tid] = payload
                turn = open_turns.get(tid)
                if turn is not None:
                    turn.model = payload.get("model") or turn.model
                    turn.cwd = payload.get("cwd") or turn.cwd
            continue

        if row_type == "event_msg" and payload.get("type") == "user_message":
            # Authoritative source for the turn's user text. A
            # response_item/message with role=user is NOT reliable for
            # this: Codex also tags injected preamble (e.g. the
            # <recommended_plugins> context block) as role=user WITH
            # matching turn_id metadata -- unlike Claude Code's isMeta
            # rows, there is no flag distinguishing real user input from
            # injected user-role context at the response_item level.
            # event_msg/user_message fires exactly once per turn with the
            # real prompt text (verified against a live `codex exec` run),
            # so it is used instead and response_item scanning below no
            # longer needs to guess.
            ts = parse_timestamp(row.get("timestamp"))
            msg = payload.get("message")
            if isinstance(msg, str) and open_turns:
                # Like MCP completion events, user_message has no turn_id.
                # It belongs to the most recently started turn; older open
                # turns are interrupted/incomplete turns retained for retry.
                turn = list(open_turns.values())[-1]
                turn.user_text = msg
                turn.user_ts = ts
                turn.user_text_authoritative = True
                for candidate_text, skill in turn.pending_skills:
                    if candidate_text != msg:
                        _record_skill(turn, skill)
                turn.pending_skills.clear()
            continue

        if row_type == "event_msg" and payload.get("type") == "token_count":
            info_blob = payload.get("info")
            ts = parse_timestamp(row.get("timestamp"))
            if isinstance(info_blob, dict) and open_turns:
                # Positional attribution: whichever turn is currently open
                # (there is at most one, since this is a single-thread
                # rollout with sequential turns) gets this usage snapshot
                # queued for its next-closed round.
                for tid in open_turns:
                    pending_usage.setdefault(tid, []).append((info_blob, ts))
            continue

        if row_type == "event_msg" and payload.get("type") == "mcp_tool_call_end":
            invocation = payload.get("invocation")
            if not isinstance(invocation, dict):
                continue
            server = invocation.get("server")
            tool = invocation.get("tool")
            if not isinstance(server, str) or not isinstance(tool, str) or not open_turns:
                continue
            # This event carries no turn_id, so it is attributed positionally.
            # A turn that ends without task_complete (e.g. an interrupted turn
            # whose task_complete is not in this read) stays in open_turns, so
            # more than one turn can be open at once; the MCP call belongs to
            # the most recently started turn, not the oldest.
            turn = list(open_turns.values())[-1]
            end_ts = parse_timestamp(row.get("timestamp"))
            call_id = payload.get("call_id")
            entry = pending_tool_calls.get(turn.turn_id, {}).get(str(call_id))
            if entry:
                call = entry[1]
                call.update({
                    "name": f"mcp__{server}__{tool}",
                    "output": _mcp_result_text(payload.get("result")),
                    "output_ts": end_ts,
                    "mcp_server": server,
                    "mcp_tool": tool,
                    "result_status": _mcp_result_status(payload.get("result")),
                })
            else:
                # No paired function_call was seen for this call_id, so there
                # is no start timestamp on record. Recover the span's start
                # from the event's own end time minus its reported duration
                # (rather than collapsing the span to zero latency).
                start_ts = _mcp_start_ts(end_ts, payload.get("duration"))
                current_round(turn).tool_calls.append({
                    "call_id": call_id,
                    "name": f"mcp__{server}__{tool}",
                    "input": invocation.get("arguments"),
                    "ts": start_ts,
                    "output": _mcp_result_text(payload.get("result")),
                    "output_ts": end_ts,
                    "mcp_server": server,
                    "mcp_tool": tool,
                    "result_status": _mcp_result_status(payload.get("result")),
                })
            continue

        if row_type == "event_msg" and payload.get("type") == "task_complete":
            tid = payload.get("turn_id")
            turn = open_turns.pop(tid, None) if isinstance(tid, str) else None
            if turn is None:
                continue
            attach_usage(turn)
            turn.last_agent_message = payload.get("last_agent_message")
            turn.completed_ts = parse_timestamp(row.get("timestamp"))
            turn.duration_ms = payload.get("duration_ms")
            turn.end_offset = row_offset
            turn.complete = True
            turns.append(turn)
            pending_tool_calls.pop(tid, None)
            pending_usage.pop(tid, None)
            continue

        if row_type != "response_item":
            continue

        item_type = payload.get("type")

        if item_type == "message":
            tid = row_turn_id(payload)
            if not tid or tid not in open_turns:
                continue
            turn = open_turns[tid]
            role = payload.get("role")
            text = extract_text(payload.get("content"))
            ts = parse_timestamp(row.get("timestamp"))
            # Both the real prompt echo and a Skill injection are user-role
            # messages, and either may arrive before event_msg/user_message.
            # Defer candidates until that authoritative prompt is known so a
            # prompt beginning with the Skill markup is not misclassified.
            skill = _skill_name_from_payload(payload) if role == "user" else None
            if skill:
                if turn.user_text_authoritative:
                    if text != turn.user_text:
                        _record_skill(turn, skill)
                else:
                    turn.pending_skills.append((text, skill))
            if role == "user" and not turn.user_text_authoritative:
                # Fallback only: response_item/message role=user rows can
                # be injected context (e.g. <recommended_plugins>) rather
                # than the real prompt -- event_msg/user_message (handled
                # above) is authoritative and, once seen, always wins. This
                # branch just keeps SOMETHING populated if that event is
                # ever missing (older/other builds), preferring the LAST
                # user-role row seen (closer in the stream to the actual
                # turn) over the first.
                turn.user_text = text
                turn.user_ts = ts
            elif role == "assistant":
                current_round(turn).assistant_texts.append((text, ts))
            continue

        if item_type == "reasoning":
            tid = row_turn_id(payload)
            if not tid or tid not in open_turns:
                continue
            turn = open_turns[tid]
            attach_usage(turn)  # close out the round that just finished
            turn.rounds.append(TurnRound(
                reasoning=payload,
                reasoning_ts=parse_timestamp(row.get("timestamp")),
            ))
            continue

        if item_type in ("function_call", "custom_tool_call"):
            tid = row_turn_id(payload)
            if not tid or tid not in open_turns:
                continue
            turn = open_turns[tid]
            ts = parse_timestamp(row.get("timestamp"))
            call = _normalize_tool_call(payload, ts)
            current_round(turn).tool_calls.append(call)
            call_id = call.get("call_id")
            if call_id:
                pending_tool_calls.setdefault(tid, {})[str(call_id)] = (item_type, call)
            continue

        if item_type in ("function_call_output", "custom_tool_call_output"):
            tid = row_turn_id(payload)
            if not tid or tid not in open_turns:
                continue
            call_id = payload.get("call_id")
            ts = parse_timestamp(row.get("timestamp"))
            bucket = pending_tool_calls.get(tid) or {}
            entry = bucket.get(str(call_id)) if call_id else None
            if entry:
                _, call = entry
                call["output"] = tool_call_output_text(payload)
                call["output_ts"] = ts
                # Only command tools own process exit codes. Other tools may
                # legitimately return an unrelated business field with the
                # same name, which must not become an execution outcome.
                exit_code = tool_call_exit_code(payload) if call.get("name") in COMMAND_TOOL_NAMES else None
                if exit_code is not None:
                    call["exit_code"] = exit_code
                    call["result_status"] = "success" if exit_code == 0 else "error"
            continue

        # Unknown response_item subtype -- ignore.

    return turns

# ----------------- One Signal ingestion-batch construction (identical wire
# shape/helpers to ../one-signal/hooks/one_signal_hook.py) -----------------
def _iso(ts: Optional[datetime]) -> Optional[str]:
    if ts is None:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")

def _event_envelope(event_type: str, body: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": str(uuid.uuid4()),
        "timestamp": _iso(datetime.now(timezone.utc)),
        "type": event_type,
        "body": {k: v for k, v in body.items() if v is not None},
    }

def _trace_create(*, trace_id: str, name: str, user_id: Optional[str], session_id: str,
                   input_: Any, output: Any, metadata: Dict[str, Any], tags: List[str],
                   timestamp: Optional[datetime]) -> Dict[str, Any]:
    return _event_envelope("trace-create", {
        "id": trace_id,
        "timestamp": _iso(timestamp),
        "name": name,
        "userId": user_id,
        "sessionId": session_id,
        "input": input_,
        "output": output,
        "metadata": metadata,
        "tags": tags,
    })

def _observation_create(*, obs_id: str, trace_id: str, parent_id: Optional[str], obs_type: str,
                         name: str, start_time: Optional[datetime], end_time: Optional[datetime],
                         input_: Any = None, output: Any = None, model: Optional[str] = None,
                         usage_details: Optional[Dict[str, int]] = None,
                         metadata: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return _event_envelope("observation-create", {
        "id": obs_id,
        "traceId": trace_id,
        "parentObservationId": parent_id,
        # Classic ingestion's observation-create `type` only accepts
        # GENERATION | SPAN | EVENT -- callers below never pass anything
        # else (same constraint as ../one-signal; a "TOOL" type is
        # silently rejected inside the batch's per-event 207 response).
        "type": obs_type,
        "name": name,
        "startTime": _iso(start_time),
        "endTime": _iso(end_time),
        "input": input_,
        "output": output,
        "model": model,
        "usageDetails": usage_details,
        "metadata": metadata,
    })

def short_session_label(thread_id: str, max_len: int = 12) -> str:
    tid = thread_id.strip()
    if not tid:
        return "unknown"
    parts = tid.split("-")
    if len(parts) == 5 and len(parts[0]) == 8:
        return parts[0]
    return tid if len(tid) <= max_len else tid[:max_len].rstrip("-")

def trace_display_name(thread_id: str, turn_num: int) -> str:
    return f"Codex CLI - Turn {turn_num} ({short_session_label(thread_id)})"

def usage_details_from_token_count(info_blob: Optional[Dict[str, Any]]) -> Optional[Dict[str, int]]:
    """Maps event_msg/token_count.payload.info.last_token_usage to Langfuse
    usageDetails. last_token_usage (not total_token_usage) is the
    incremental usage for the model call that just returned -- verified
    against a real rollout (input/output/cached/reasoning token fields)."""
    if not isinstance(info_blob, dict):
        return None
    usage = info_blob.get("last_token_usage")
    if not isinstance(usage, dict):
        return None
    details: Dict[str, int] = {}
    for src, dst in (
        ("input_tokens", "input"),
        ("output_tokens", "output"),
        ("cached_input_tokens", "cache_read_input_tokens"),
        ("reasoning_output_tokens", "reasoning_output_tokens"),
    ):
        v = usage.get(src)
        if isinstance(v, int) and v > 0:
            details[dst] = v
    return details or None

def build_turn_events(thread_id: str, turn_num: int, turn: Turn, rollout_path: Path,
                       user_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """Builds the ingestion-batch events for one turn: trace-create, root
    SPAN, one GENERATION per model round (+ nested tool SPANs and reasoning
    EVENTs)."""
    events: List[Dict[str, Any]] = []

    user_text, user_text_meta = truncate_text(turn.user_text)
    final_text_raw = turn.last_agent_message or (
        turn.rounds[-1].assistant_texts[-1][0] if turn.rounds and turn.rounds[-1].assistant_texts else ""
    )
    final_text, _ = truncate_text(final_text_raw)

    turn_end_ts = turn.completed_ts
    turn_metadata: Dict[str, Any] = {
        "source": "codex-cli",
        "thread_id": thread_id,
        "turn_id": turn.turn_id,
        "turn_number": turn_num,
        "rollout_path": str(rollout_path),
        "user_text": user_text_meta,
        "round_count": len(turn.rounds),
    }
    if turn.cwd:
        turn_metadata["cwd"] = turn.cwd
    if turn.duration_ms is not None:
        turn_metadata["duration_ms"] = turn.duration_ms

    if turn.skills:
        turn_metadata["skill_names"] = turn.skills
    mcp_tools = [
        {"server": tc["mcp_server"], "tool": tc["mcp_tool"]}
        for rnd in turn.rounds for tc in rnd.tool_calls
        if tc.get("mcp_server") and tc.get("mcp_tool")
    ]
    tags = list(dict.fromkeys([
        "codex-cli",
        *[f"skill:{name}" for name in turn.skills],
        *[f"mcp:{item['server']}:{item['tool']}" for item in mcp_tools],
    ]))

    trace_id = f"{thread_id}-t{turn_num}"
    root_obs_id = f"{trace_id}-root"

    events.append(_trace_create(
        trace_id=trace_id,
        name=trace_display_name(thread_id, turn_num),
        user_id=user_id,
        session_id=thread_id,
        input_={"role": "user", "content": user_text},
        output={"role": "assistant", "content": final_text},
        metadata=turn_metadata,
        tags=tags,
        timestamp=turn.user_ts,
    ))
    events.append(_observation_create(
        obs_id=root_obs_id,
        trace_id=trace_id,
        parent_id=None,
        obs_type="SPAN",
        name=f"Turn {turn_num}",
        start_time=turn.user_ts,
        end_time=turn_end_ts or turn.user_ts,
        input_={"role": "user", "content": user_text},
        output={"role": "assistant", "content": final_text},
        metadata=turn_metadata,
    ))

    prev_ts = turn.user_ts
    for r_idx, rnd in enumerate(turn.rounds):
        gen_id = f"{root_obs_id}-gen{r_idx + 1}"

        am_texts = [t for t, _ in rnd.assistant_texts if t]
        am_text, am_text_meta = truncate_text("\n\n".join(am_texts))
        am_ts = rnd.assistant_texts[-1][1] if rnd.assistant_texts else (rnd.reasoning_ts or prev_ts)

        gen_output: Dict[str, Any] = {"role": "assistant"}
        if am_text:
            gen_output["content"] = am_text
        if rnd.tool_calls:
            gen_output["tool_calls"] = [
                {"id": tc.get("call_id"), "name": tc.get("name")}
                for tc in rnd.tool_calls
            ]

        end_candidates = [t for t in [am_ts] if t is not None]
        for tc in rnd.tool_calls:
            if tc.get("output_ts") is not None:
                end_candidates.append(tc["output_ts"])
        gen_end_ts = max(end_candidates) if end_candidates else prev_ts

        events.append(_observation_create(
            obs_id=gen_id,
            trace_id=trace_id,
            parent_id=root_obs_id,
            obs_type="GENERATION",
            name=f"Codex Generation {r_idx + 1}",
            start_time=prev_ts or rnd.reasoning_ts,
            end_time=gen_end_ts or prev_ts,
            input_=({"role": "user", "content": user_text} if r_idx == 0 else None),
            output=gen_output,
            model=turn.model,
            usage_details=usage_details_from_token_count(rnd.usage),
            metadata={
                "round_index": r_idx,
                "assistant_text": am_text_meta,
                "tool_count": len(rnd.tool_calls),
                "has_reasoning": rnd.reasoning is not None,
            },
        ))

        if rnd.reasoning is not None:
            # `summary` can be a list of plain strings OR a list of
            # {"type": "summary_text", "text": ...} blocks depending on
            # build/config (both observed against real rollouts) --
            # extract_text() handles either shape.
            summary_text = extract_text(rnd.reasoning.get("summary")) or None
            events.append(_observation_create(
                obs_id=f"{gen_id}-reasoning",
                trace_id=trace_id,
                parent_id=gen_id,
                obs_type="EVENT",
                name="Reasoning",
                start_time=rnd.reasoning_ts,
                end_time=rnd.reasoning_ts,
                # encrypted_content is opaque ciphertext -- intentionally
                # never read or forwarded here.
                output=({"summary": summary_text} if summary_text else None),
                metadata={
                    "has_encrypted_content": bool(rnd.reasoning.get("encrypted_content")),
                    "reasoning_id": rnd.reasoning.get("id"),
                },
            ))

        for t_idx, tc in enumerate(rnd.tool_calls):
            tinput, tinput_meta = truncate_text(
                tc.get("input") if isinstance(tc.get("input"), str) else json.dumps(tc.get("input"), ensure_ascii=False) if tc.get("input") is not None else None
            )
            toutput, toutput_meta = truncate_text(tc.get("output"))
            events.append(_observation_create(
                obs_id=f"{gen_id}-tool{t_idx + 1}",
                trace_id=trace_id,
                parent_id=gen_id,
                obs_type="SPAN",
                name=f"Tool: {tc.get('name')}",
                start_time=tc.get("ts") or am_ts,
                end_time=tc.get("output_ts") or tc.get("ts") or am_ts,
                input_=tinput or None,
                output=toutput or None,
                metadata={
                    "tool_name": tc.get("name"),
                    "tool_id": tc.get("call_id"),
                    "result_status": tc.get("result_status", "unknown"),
                    **({"exit_code": tc["exit_code"]} if "exit_code" in tc else {}),
                    **({
                        "mcp_server": tc["mcp_server"],
                        "mcp_tool": tc["mcp_tool"],
                    } if tc.get("mcp_server") and tc.get("mcp_tool") else {}),
                    "input_meta": tinput_meta,
                    "output_meta": toutput_meta,
                },
            ))

        prev_ts = gen_end_ts or prev_ts

    return events

# ----------------- Chunking (identical to ../one-signal) -----------------
def _event_size_bytes(event: Dict[str, Any]) -> int:
    return len(json.dumps(event, ensure_ascii=False).encode("utf-8"))

def chunk_indices(events: List[Dict[str, Any]], max_events: int = MAX_EVENTS_PER_BATCH,
                   max_bytes: int = MAX_BYTES_PER_BATCH) -> List[List[int]]:
    groups: List[List[int]] = []
    current: List[int] = []
    current_bytes = 0
    envelope_slack = 2048
    budget = max(max_bytes - envelope_slack, 1)

    for i, event in enumerate(events):
        size = _event_size_bytes(event)
        if current and (len(current) >= max_events or current_bytes + size > budget):
            groups.append(current)
            current = []
            current_bytes = 0
        current.append(i)
        current_bytes += size

    if current:
        groups.append(current)

    return groups

# ----------------- Transport (identical to ../one-signal) -----------------
def _extract_ingestion_errors(body: bytes) -> List[Dict[str, Any]]:
    try:
        parsed = json.loads(body.decode("utf-8", errors="replace"))
    except Exception:
        return []
    if not isinstance(parsed, dict):
        return []
    errors = parsed.get("errors")
    if not isinstance(errors, list):
        return []
    return [e for e in errors if isinstance(e, dict)]

def post_batch(events: List[Dict[str, Any]], base_url: str, api_token: str,
               metadata: Dict[str, Any]) -> bool:
    url = base_url.rstrip("/") + "/api/v1/observe/ingest"
    payload = json.dumps({"batch": events, "metadata": metadata}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        method="POST",
        headers={
            "Authorization": f"Bearer {api_token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            status = resp.getcode()
            body = resp.read()
            if status == 207:
                errors = _extract_ingestion_errors(body)
                for err in errors:
                    warning(
                        "ingest event failed: "
                        f"id={err.get('id')} status={err.get('status')} message={err.get('message')}"
                    )
                debug(f"ingest ok: status={status} events={len(events)} failed={len(errors)}")
                return len(errors) == 0
            elif 200 <= status < 300:
                debug(f"ingest ok: status={status} events={len(events)}")
                return True
            else:
                info(f"ingest unexpected status {status}: {body[:500]!r}")
                return False
    except urllib.error.HTTPError as e:
        body = e.read()
        if e.code == 503:
            code = None
            try:
                parsed = json.loads(body.decode("utf-8", errors="replace"))
                if isinstance(parsed, dict):
                    err = parsed.get("error")
                    code = err.get("code") if isinstance(err, dict) else err
            except Exception:
                pass
            if code == "signal_not_configured":
                debug(
                    "One Signal is not connected for this organization yet -- "
                    "connect Langfuse in Console -> Integrations. Dropping this batch."
                )
                return False
        info(f"ingest failed: HTTP {e.code}: {body[:500]!r}")
        return False
    except urllib.error.URLError as e:
        debug(f"ingest network error: {e}")
        return False
    except Exception as e:
        debug(f"ingest request failed unexpectedly: {type(e).__name__}: {e}")
        return False

def deliver(events: List[Dict[str, Any]], event_turn_idx: List[int], base_url: str, api_token: str) -> set:
    chunk_idx_groups = chunk_indices(events)
    debug(f"delivering {len(events)} events in {len(chunk_idx_groups)} chunk(s)")

    turn_ok: Dict[int, bool] = {}
    for i, idx_group in enumerate(chunk_idx_groups):
        chunk = [events[j] for j in idx_group]
        fully_accepted = post_batch(chunk, base_url, api_token, metadata={
            "sdk_name": "one-signal-codex-hook",
            "sdk_version": PLUGIN_VERSION,
            "chunk_index": i,
            "chunk_count": len(chunk_idx_groups),
        })
        if not fully_accepted:
            info(f"chunk {i + 1}/{len(chunk_idx_groups)} failed to deliver ({len(chunk)} events)")
        for j in idx_group:
            t = event_turn_idx[j]
            turn_ok[t] = turn_ok.get(t, True) and fully_accepted

    return {t for t, ok in turn_ok.items() if ok}

# ----------------- Config -----------------
def load_config_file() -> Dict[str, Any]:
    try:
        if not CONFIG_FILE.exists():
            return {}
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        debug(f"load_config_file failed: {e}")
        return {}

def resolve_config() -> Tuple[str, str, Optional[str]]:
    """env vars first, then ~/.codex/one-signal.json. Never logs the token."""
    file_cfg = load_config_file()
    base_url = os.environ.get("ONE_SIGNAL_BASE_URL") or file_cfg.get("ONE_SIGNAL_BASE_URL") or "https://connector.1infra.io"
    api_token = os.environ.get("ONE_SIGNAL_API_TOKEN") or file_cfg.get("ONE_SIGNAL_API_TOKEN") or ""
    user_id = os.environ.get("ONE_SIGNAL_USER_ID") or file_cfg.get("ONE_SIGNAL_USER_ID") or None
    return base_url, api_token, user_id

# ----------------- Main -----------------
def main(argv: List[str]) -> int:
    if len(argv) > 1 and argv[1] == "--self-test":
        return run_self_test()

    start = time.time()
    debug("Hook started")

    base_url, api_token, user_id = resolve_config()
    if not api_token:
        debug("Missing ONE_SIGNAL_API_TOKEN; exiting without emitting.")
        return 0

    # Two supported transports feed the same pipeline:
    #   - legacy `notify` argv (install.py path): thread-id, no rollout path;
    #   - plugin `Stop` hook stdin: session_id + transcript_path directly.
    # argv/stdin reads are lock-free; the rollout is resolved under the lock.
    notify = read_notify_payload(argv)
    thread_id = extract_thread_id(notify)
    transcript_hint: Optional[str] = None
    if not thread_id:
        stop = read_stop_payload()
        thread_id = extract_thread_id(stop)
        transcript_hint = stop.get("transcript_path") or stop.get("transcript-path")

    emitted = 0
    committed = 0
    total_events = 0
    try:
        with FileLock(LOCK_FILE):
            state = load_state()
            rollout_path = resolve_rollout(thread_id, transcript_hint, state)
            if rollout_path is None:
                save_state(state)
                debug(
                    "could not resolve rollout path "
                    f"(thread_id={thread_id!r}, transcript_hint={transcript_hint!r})"
                )
                return 0
            if not thread_id:
                thread_id = thread_id_from_path(rollout_path) or "unknown-session"

            key = state_key(thread_id, str(rollout_path))
            entry = state.get(key, {})
            offset = int(entry.get("offset", 0))
            turn_count = int(entry.get("turn_count", 0))

            rows, turns = read_completed_turns_after_flush(rollout_path, offset)
            if not rows:
                save_state(state)
                return 0

            if not turns:
                save_state(state)
                return 0

            emitted = len(turns)

            events: List[Dict[str, Any]] = []
            event_turn_idx: List[int] = []
            for i, t in enumerate(turns):
                turn_num = turn_count + i + 1
                try:
                    turn_events = build_turn_events(thread_id, turn_num, t, rollout_path, user_id=user_id)
                except Exception as e:
                    info(f"build_turn_events failed: {type(e).__name__}: {e}")
                    turn_events = []
                events.extend(turn_events)
                event_turn_idx.extend([i] * len(turn_events))

            total_events = len(events)
            accepted_idx = deliver(events, event_turn_idx, base_url, api_token) if events else set()

            for i in range(len(turns)):
                if i in accepted_idx:
                    committed = i + 1
                else:
                    break

            if committed < emitted:
                info(
                    f"only {committed}/{emitted} turn(s) fully accepted upstream this run; "
                    "checkpoint held back so the rest retry next time"
                )

            if committed > 0:
                offset = turns[committed - 1].end_offset
                turn_count += committed

            state[key] = {
                "offset": offset,
                "turn_count": turn_count,
                "updated": datetime.now(timezone.utc).isoformat(),
            }
            save_state(state)

        dur = time.time() - start
        info(
            f"Processed {committed}/{emitted} turns ({total_events} events) in "
            f"{dur:.2f}s (thread_id={thread_id})"
        )
        return 0

    except TimeoutError as e:
        debug(f"lock timeout, skipping: {e}")
        return 0
    except Exception as e:
        debug(f"Unexpected failure: {e}")
        return 0

# ----------------- Self-test (no network; see test_hook.py for the
# pytest-style wrapper around this same function) -----------------
def run_self_test() -> int:
    fixture_path = Path(__file__).resolve().parent / "fixtures" / "sample_rollout.jsonl"
    if not fixture_path.exists():
        print(f"FAIL: fixture not found: {fixture_path}", file=sys.stderr)
        return 1

    thread_id = "0196f5aa-aaaa-7000-8000-000000000001"
    rows = read_new_lines(fixture_path, 0)
    turns = build_turns(rows)
    ok = True

    def check(cond: bool, label: str) -> None:
        nonlocal ok
        status = "ok" if cond else "FAIL"
        if not cond:
            ok = False
        print(f"[{status}] {label}")

    check(len(turns) == 1, f"exactly one complete turn parsed (got {len(turns)})")
    if not turns:
        return 1

    events = build_turn_events(thread_id, 1, turns[0], fixture_path)
    types = [e["body"].get("type") for e in events]
    check(sum(1 for e in events if e["type"] == "trace-create") == 1, "exactly one trace-create")
    check(any(t == "SPAN" and e["body"].get("name") == "Turn 1" for e, t in zip(events, types)),
          "root SPAN 'Turn 1' present")

    generations = [e for e in events if e["body"].get("type") == "GENERATION"]
    check(len(generations) >= 1, f"at least one GENERATION (got {len(generations)})")
    check(any(g["body"].get("model") == "gpt-test-model" for g in generations),
          "a GENERATION carries model from turn_context")
    check(any(g["body"].get("usageDetails") for g in generations),
          "a GENERATION carries usageDetails from token_count")

    tool_spans = [e for e in events if e["body"].get("type") == "SPAN" and "tool_name" in (e["body"].get("metadata") or {})]
    check(len(tool_spans) >= 1, f"at least one tool SPAN (got {len(tool_spans)})")
    check(any(s["body"]["metadata"].get("tool_name") == "shell" for s in tool_spans),
          "a tool SPAN carries metadata.tool_name == 'shell'")
    check(any("hi" in (s["body"].get("output") or "") for s in tool_spans),
          "the shell tool SPAN's output includes the function_call_output text")

    reasoning_events = [e for e in events if e["body"].get("type") == "EVENT" and e["body"].get("name") == "Reasoning"]
    check(len(reasoning_events) >= 1, f"at least one reasoning EVENT (got {len(reasoning_events)})")

    serialized = json.dumps(events)
    # Substring-match the JSON *key* (quoted, colon-terminated) rather than
    # bare "encrypted_content" -- metadata.has_encrypted_content is a
    # legitimate boolean flag whose name contains that substring.
    check('"encrypted_content":' not in serialized, "no encrypted_content field leaked into any event")
    check("ENCRYPTED_BLOB_DO_NOT_SEND" not in serialized, "the fixture's ciphertext marker never appears in output")

    trace = next(e for e in events if e["type"] == "trace-create")
    check(trace["body"].get("input", {}).get("content") == "hello world", "trace input captures the user turn text")
    check(trace["body"].get("output", {}).get("content") == "Done, printed hi", "trace output captures last_agent_message")

    print()
    if ok:
        print(f"SELF-TEST PASSED ({len(events)} events built from {len(turns)} turn(s))")
        return 0
    print("SELF-TEST FAILED", file=sys.stderr)
    return 1

if __name__ == "__main__":
    sys.exit(main(sys.argv))
