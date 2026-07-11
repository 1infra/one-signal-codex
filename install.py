#!/usr/bin/env python3
"""
One-shot installer for the Codex CLI One Signal hook.

    python3 install.py --token oc_xxx [--base-url https://connector.1infra.io] [--user-id you@example.com]
    python3 install.py --uninstall

What it does:
  1. Writes {CODEX_HOME:-~/.codex}/one-signal.json (chmod 600) with the
     token/base-url/user-id. Config resolution at hook-run time is env vars
     first, then this file -- see one_signal_codex_hook.py.
  2. Idempotently adds or updates a top-level `notify = ["python3", "<abs
     path to one_signal_codex_hook.py>"]` line in
     {CODEX_HOME:-~/.codex}/config.toml. If a DIFFERENT notify entry already
     exists (not ours), this refuses to touch it and prints the existing
     value plus manual chaining instructions -- Codex only runs ONE notify
     command, so silently overwriting a user's existing notify integration
     would break it.
  3. Never prints the token.

This is a deliberately lightweight, line-based config.toml editor, not a
full TOML round-trip parser/writer (Python's stdlib has no TOML writer, and
pulling in a third-party one for a fast v1 installer isn't worth it). It
only recognizes a `notify = [...]` array that starts at column 0 (a
top-level key, uncommented) -- which is where Codex requires it to live
anyway, since TOML keys must precede the first `[table]` header to be
top-level. Anything unusual about your config.toml's notify entry (e.g. it
being written across a very unusual line-wrapping) may require a manual
edit; this script tells you so rather than guessing.
"""

import argparse
import json
import os
import re
import stat
import sys
from pathlib import Path

HOOK_FILENAME = "one_signal_codex_hook.py"
DEFAULT_BASE_URL = "https://connector.1infra.io"


def codex_home() -> Path:
    override = os.environ.get("CODEX_HOME")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".codex"


def hook_script_path() -> Path:
    return (Path(__file__).resolve().parent / HOOK_FILENAME).resolve()


def write_config_file(codex_home_dir: Path, token: str, base_url: str, user_id: str | None) -> Path:
    config_path = codex_home_dir / "one-signal.json"
    data = {"ONE_SIGNAL_API_TOKEN": token, "ONE_SIGNAL_BASE_URL": base_url}
    if user_id:
        data["ONE_SIGNAL_USER_ID"] = user_id
    codex_home_dir.mkdir(parents=True, exist_ok=True)
    tmp = config_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)  # 0600
    os.replace(tmp, config_path)
    return config_path


# --- Minimal line-based TOML notify editor ---

_TOP_LEVEL_NOTIFY_RE = re.compile(r"^notify\s*=\s*\[")
_SECTION_HEADER_RE = re.compile(r"^\[")


def _find_notify_block(lines: list[str]) -> tuple[int, int] | None:
    """Returns (start_line_idx, end_line_idx_inclusive) of an existing
    top-level `notify = [...]` array, scanning only lines BEFORE the first
    `[section]` header (top-level keys can't legally appear after one).
    Handles a notify array that wraps across multiple lines by tracking
    bracket depth. Returns None if not found."""
    for i, line in enumerate(lines):
        if _SECTION_HEADER_RE.match(line):
            return None  # reached the first table; no top-level notify before it
        if _TOP_LEVEL_NOTIFY_RE.match(line):
            depth = line.count("[") - line.count("]")
            end = i
            while depth > 0 and end + 1 < len(lines):
                end += 1
                depth += lines[end].count("[") - lines[end].count("]")
            return (i, end)
    return None


def _first_section_header_index(lines: list[str]) -> int | None:
    for i, line in enumerate(lines):
        if _SECTION_HEADER_RE.match(line):
            return i
    return None


def update_config_toml(config_toml_path: Path, hook_path: Path, dry_run: bool = False) -> tuple[bool, str]:
    """Returns (changed, message)."""
    notify_line = f'notify = ["python3", "{hook_path}"]\n'

    if not config_toml_path.exists():
        config_toml_path.parent.mkdir(parents=True, exist_ok=True)
        if not dry_run:
            config_toml_path.write_text(notify_line, encoding="utf-8")
        return True, f"created {config_toml_path} with a new notify entry"

    text = config_toml_path.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)

    block = _find_notify_block(lines)
    if block is not None:
        start, end = block
        existing = "".join(lines[start:end + 1])
        if HOOK_FILENAME in existing:
            if existing == notify_line:
                return False, "already installed and up to date -- nothing to do"
            new_lines = lines[:start] + [notify_line] + lines[end + 1:]
            if not dry_run:
                config_toml_path.write_text("".join(new_lines), encoding="utf-8")
            return True, f"updated existing notify entry (was pointed at a different path):\n  old: {existing.strip()}\n  new: {notify_line.strip()}"
        else:
            raise RefuseToClobber(existing.strip())

    insert_at = _first_section_header_index(lines)
    if insert_at is None:
        new_lines = lines + (["\n"] if lines and not lines[-1].endswith("\n") else []) + [notify_line]
    else:
        new_lines = lines[:insert_at] + [notify_line, "\n"] + lines[insert_at:]
    if not dry_run:
        config_toml_path.write_text("".join(new_lines), encoding="utf-8")
    return True, f"added a new notify entry to {config_toml_path}"


class RefuseToClobber(Exception):
    pass


def remove_notify_entry(config_toml_path: Path) -> tuple[bool, str]:
    if not config_toml_path.exists():
        return False, "config.toml does not exist -- nothing to remove"
    text = config_toml_path.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    block = _find_notify_block(lines)
    if block is None:
        return False, "no top-level notify entry found -- nothing to remove"
    start, end = block
    existing = "".join(lines[start:end + 1])
    if HOOK_FILENAME not in existing:
        return False, (
            "the existing notify entry does not point at this hook -- leaving it alone:\n"
            f"  {existing.strip()}"
        )
    new_lines = lines[:start] + lines[end + 1:]
    config_toml_path.write_text("".join(new_lines), encoding="utf-8")
    return True, "removed the notify entry"


def cmd_install(args: argparse.Namespace) -> int:
    home = codex_home()
    hook_path = hook_script_path()
    if not hook_path.exists():
        print(f"ERROR: hook script not found next to install.py: {hook_path}", file=sys.stderr)
        return 1

    config_path = write_config_file(home, args.token, args.base_url, args.user_id)
    print(f"wrote {config_path} (chmod 600)")

    config_toml = home / "config.toml"
    try:
        changed, message = update_config_toml(config_toml, hook_path)
    except RefuseToClobber as e:
        print(
            "ERROR: an existing `notify` entry in "
            f"{config_toml} does not point at this hook -- refusing to overwrite it:\n"
            f"  {e}\n\n"
            "Codex only runs ONE notify command, so you need to chain them manually.\n"
            "Create a small wrapper script that calls both, e.g.:\n\n"
            "  #!/bin/sh\n"
            f"  python3 {hook_path} \"$@\" &\n"
            "  <your other notify command> \"$@\"\n\n"
            "then point `notify` in config.toml at that wrapper instead.",
            file=sys.stderr,
        )
        return 1

    print(message)
    print()
    print("Install complete.")
    print(f"  hook script : {hook_path}")
    print(f"  config file : {config_path}")
    print(f"  base url    : {args.base_url}")
    print("  api token   : (hidden)")
    print()
    print("Next: run one Codex CLI turn (interactive `codex` or `codex exec \"...\"`),")
    print("then check Console -> Observe -> Traces for a 'Codex CLI - Turn 1 (...)' trace.")
    print(f"Debug: set ONE_SIGNAL_CODEX_DEBUG=1 and tail {home / 'one-signal-hook.log'}")
    return 0


def cmd_uninstall(args: argparse.Namespace) -> int:
    home = codex_home()
    config_toml = home / "config.toml"
    changed, message = remove_notify_entry(config_toml)
    print(message)
    config_path = home / "one-signal.json"
    if config_path.exists():
        print(f"note: {config_path} was left in place; delete it manually if you want the token gone too:")
        print(f"  rm {config_path}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Install/uninstall the Codex CLI One Signal hook.")
    parser.add_argument("--token", help="One Connector access token (oc_...)")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help=f"One Connector deployment URL (default: {DEFAULT_BASE_URL})")
    parser.add_argument("--user-id", default=None, help="Optional user identifier attached to every trace")
    parser.add_argument("--uninstall", action="store_true", help="Remove the notify wiring (leaves the token file in place)")
    args = parser.parse_args()

    if args.uninstall:
        return cmd_uninstall(args)

    if not args.token:
        parser.error("--token is required (unless --uninstall)")
    if not args.token.startswith("oc_"):
        print(f"warning: token does not start with 'oc_' -- proceeding anyway ({args.token[:6]}...)", file=sys.stderr)

    return cmd_install(args)


if __name__ == "__main__":
    sys.exit(main())
