# Release smoke — one-signal-codex

Run this **after every version bump**, before announcing a release (marketplace
upgrade notes, changelog, Discord/Slack, etc.). It exercises the live plugin
path: a real `codex exec` turn that includes a **failing tool call** and
**fake secrets**, then a **Console API** checklist for observation shape,
ERROR level, redaction, and cost.

Automated unit tests (`test_hook.py`, fixtures under `fixtures/`) stay
required; this procedure is the field gate that unit-green alone missed
before (real exec-mode rollouts vs synthetic fixtures).

## Prerequisites

1. Codex CLI ≥ 0.144 with the `hooks` feature stable/enabled.
2. Plugin installed and enabled (`one-signal-codex@one-infra`).
3. `~/.codex/one-signal.json` (or `ONE_SIGNAL_API_TOKEN`) points at a real
   org with OTLP ingest access.
4. `codex` on `PATH` (override with `CODEX_BIN`).
5. Browser access to Console → Observe for the same org (verification uses
   **session cookies**, not the plugin `oc_` token).

## What the script does

```bash
./scripts/release-smoke.sh
# optional:
CONNECTOR_URL=https://connector.1infra.io ./scripts/release-smoke.sh
```

`scripts/release-smoke.sh`:

1. Creates a temp workdir.
2. Runs `codex exec` with a fixed prompt that:
   - Writes `smoke-secrets.txt` containing:
     - `AKIAIOSFODNN7EXAMPLE` (AWS docs example key — not a real secret)
     - `postgres://user:password@host` (URI shape the redactor must mask)
   - Runs `ls /nonexistent-smoke` (must fail; drives `level=ERROR` on the
     tool observation)
   - Echoes a unique smoke tag for easy lookup
3. Prints a verification checklist plus a parameterized `curl` / `jq` /
   small Python assert template against the Console Observe API.

The script **does not** call Console APIs itself: auth is browser-session
based (`credentials: "include"` + `Cookie` + `x-organization-id`).

## Verification gates

Against `GET ${CONNECTOR_URL}/api/v1/observe/traces/:traceId` (Console
session auth):

| # | Gate | Why |
| --- | --- | --- |
| 1 | Observation tree node count **> 1** | Turn uploaded with nested generations/tools, not a bare stub |
| 2 | At least one observation with `type == "TOOL"` and `level == "ERROR"` | Failed exec (`ls /nonexistent-smoke`) surfaces as ERROR |
| 3 | Payload does **not** contain `AKIAIOSFODNN7EXAMPLE` | Pre-upload secret redaction |
| 4 | Payload does **not** contain `postgres://user:password@host` | DB URI redaction |
| 5 | `totalCostUsd` **> 0** | Usage/cost plumbing still wired |

All five must pass before you announce the version.

### Auth note (Console API)

Observe read routes live under `/api/v1/observe/*` and require a logged-in
Console actor:

- Header `x-organization-id: <org-uuid>`
- Cookie from a browser session on the same deployment (DevTools → Network
  → any authenticated request → copy `Cookie`)
- Base URL: `CONNECTOR_URL` (default `https://connector.1infra.io`; local
  dev often `http://127.0.0.1:3001`)

Do **not** use the plugin ingest token (`oc_…`) for these GETs — ingest
scope and Console session auth are different planes.

### Snippet template

Printed at the end of `release-smoke.sh` (values filled with your
`CONNECTOR_URL` and the smoke tag). Condensed form:

```bash
export CONNECTOR_URL="${CONNECTOR_URL:-https://connector.1infra.io}"
export ORG_ID="..."
export COOKIE="..."          # browser Cookie header value
export TRACE_ID="..."        # from list or Console UI

curl -sS -G "${CONNECTOR_URL}/api/v1/observe/traces" \
  --data-urlencode "limit=10" \
  -H "x-organization-id: ${ORG_ID}" \
  -H "Cookie: ${COOKIE}" | jq .

curl -sS "${CONNECTOR_URL}/api/v1/observe/traces/${TRACE_ID}" \
  -H "x-organization-id: ${ORG_ID}" \
  -H "Cookie: ${COOKIE}" | tee /tmp/release-smoke-trace.json | jq .
# then run the assert block printed by the script (tree count, TOOL ERROR,
# no secret strings, nonzero cost)
```

## After a pass

- Record the smoke tag / trace id in the release notes PR or commit message
  if useful.
- Announce the version (marketplace upgrade, changelog).
- On failure: do **not** announce; fix, bump patch again, re-run smoke.

## Related

- Unit / fixture self-test: `uv run python test_hook.py -v` or
  `uv run python one_signal_codex_hook.py --self-test`
- Real failed-exec fixture: `fixtures/rollout-failed-exec-e2e.jsonl`
