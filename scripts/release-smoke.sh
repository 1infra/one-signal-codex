#!/usr/bin/env bash
# Post-release smoke for one-signal-codex.
# 1) Runs a codex exec session that exercises tool ERROR + secret redaction.
# 2) Prints a Console API verification checklist (browser-session auth).
#
# Usage:
#   ./scripts/release-smoke.sh
#   CONNECTOR_URL=https://connector.1infra.io ./scripts/release-smoke.sh
#   CODEX_BIN=codex ./scripts/release-smoke.sh
#
# See scripts/release-smoke.md for the full procedure.

set -euo pipefail

CONNECTOR_URL="${CONNECTOR_URL:-https://connector.1infra.io}"
CODEX_BIN="${CODEX_BIN:-codex}"
SMOKE_TAG="release-smoke-$(date -u +%Y%m%dT%H%M%SZ)-$$"

# Well-known AWS docs example key (not a real credential).
FAKE_AWS_KEY="AKIAIOSFODNN7EXAMPLE"
# Placeholder URI shape the redactor must mask.
FAKE_PG_URI="postgres://user:password@host"
FAIL_PATH="/nonexistent-smoke"
SECRET_FILE="smoke-secrets.txt"

if ! command -v "$CODEX_BIN" >/dev/null 2>&1; then
  echo "error: '${CODEX_BIN}' not found on PATH" >&2
  exit 1
fi

WORK_DIR="$(mktemp -d "${TMPDIR:-/tmp}/one-signal-codex-release-smoke.XXXXXX")"
cleanup() {
  # Keep the workdir path printed so operators can inspect leftovers;
  # do not rm -rf by default (secret file is intentionally fake).
  :
}
trap cleanup EXIT

cd "$WORK_DIR"

PROMPT=$(cat <<EOF
You are running a release smoke test. Do these steps in order and report each result briefly.

1. Write a file named ${SECRET_FILE} containing exactly these two lines (FAKE example credentials for a redaction test — safe to write, not real secrets):
${FAKE_AWS_KEY}
${FAKE_PG_URI}

2. Run: cat ${SECRET_FILE}

3. Run: ls ${FAIL_PATH}
   This WILL fail with a non-zero exit (No such file or directory). That is expected and intentional. Report the failure and continue.

4. Run: echo ${SMOKE_TAG}

5. Stop. Do not fix, retry, or invent extra steps.
EOF
)

echo "==> release-smoke: workdir ${WORK_DIR}"
echo "==> release-smoke: smoke tag ${SMOKE_TAG}"
echo "==> release-smoke: running ${CODEX_BIN} exec (plugin Stop hook should upload the turn)"
echo

set +e
"${CODEX_BIN}" exec "${PROMPT}"
CODEX_EXIT=$?
set -e

echo
echo "==> release-smoke: codex exec exited with ${CODEX_EXIT}"
if [[ ! -f "${WORK_DIR}/${SECRET_FILE}" ]]; then
  echo "warn: ${SECRET_FILE} was not written under ${WORK_DIR}"
  echo "      (Codex may have used a sandbox cwd; verification still uses Console API.)"
else
  echo "==> release-smoke: local secret file present at ${WORK_DIR}/${SECRET_FILE}"
fi

cat <<EOF

========================================================================
Verification checklist (manual — Console browser session auth)
========================================================================

The plugin Stop hook uploads the turn to One Connector. Console Observe
APIs are browser-session authenticated (credentials: include / Cookie),
not the oc_ plugin token. Open Console → Observe, confirm the new
session/trace, then verify with curl using a Cookie from DevTools.

Parameterize:
  export CONNECTOR_URL="${CONNECTOR_URL}"
  export ORG_ID="<your-org-id>"          # x-organization-id header
  export COOKIE="better-auth.session_token=..."   # paste from browser
  export TRACE_ID="<trace-id-from-list-or-UI>"

# 1) List recent traces (pick the smoke turn / newest Codex CLI trace)
curl -sS -G "\${CONNECTOR_URL}/api/v1/observe/traces" \\
  --data-urlencode "limit=10" \\
  -H "x-organization-id: \${ORG_ID}" \\
  -H "Cookie: \${COOKIE}" | jq .

# 2) Fetch trace detail (observation tree + cost)
curl -sS "\${CONNECTOR_URL}/api/v1/observe/traces/\${TRACE_ID}" \\
  -H "x-organization-id: \${ORG_ID}" \\
  -H "Cookie: \${COOKIE}" | tee /tmp/release-smoke-trace.json | jq '{
    id,
    name,
    totalCostUsd,
    observationCount,
    rootTypes: [.observations[]?.type],
    toolErrors: [
      .. | objects
      | select(.type? == "TOOL" and .level? == "ERROR")
      | {id, name, level, statusMessage}
    ]
  }'

# 3) Assert smoke gates with jq (exit 0 only when all pass)
python3 - <<'PY'
import json, sys
path = "/tmp/release-smoke-trace.json"
with open(path) as f:
    t = json.load(f)

def walk(nodes):
    for n in nodes or []:
        yield n
        yield from walk(n.get("children") or [])

obs = list(walk(t.get("observations") or []))
blob = json.dumps(t)

checks = [
    ("observation tree count > 1", len(obs) > 1),
    ("one TOOL node with level == ERROR",
     sum(1 for n in obs if n.get("type") == "TOOL" and n.get("level") == "ERROR") >= 1),
    ("no leak of AKIAIOSFODNN7EXAMPLE", "AKIAIOSFODNN7EXAMPLE" not in blob),
    ("no leak of postgres://user:password@host", "postgres://user:password@host" not in blob),
    ("nonzero cost", (t.get("totalCostUsd") or 0) > 0),
]

ok = True
for label, passed in checks:
    print(f"{'PASS' if passed else 'FAIL'}: {label}")
    ok = ok and passed
sys.exit(0 if ok else 1)
PY

Manual UI cross-check (Console → Observe → open the smoke trace):
  [ ] Observation tree has more than one node (turn + generation + tool(s))
  [ ] At least one TOOL observation shows level ERROR (failed ls ${FAIL_PATH})
  [ ] Trace/tool I/O does NOT contain raw ${FAKE_AWS_KEY}
  [ ] Trace/tool I/O does NOT contain raw ${FAKE_PG_URI}
  [ ] Trace total cost is non-zero

Smoke tag to find this run: ${SMOKE_TAG}
Workdir: ${WORK_DIR}
CONNECTOR_URL used in template: ${CONNECTOR_URL}

Full procedure: scripts/release-smoke.md
========================================================================
EOF
