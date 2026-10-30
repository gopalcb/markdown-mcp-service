---
file-group: worksummary
---
# Markdown MCP consent origin fix
Status: Complete locally; deployment and private host acceptance not performed.
Follow-up task: 71c76857-e273-4cea-a4b8-996e4221a635 (remote initial status pending).
Parent OAuth task: 74364279-b4ca-4ec9-835e-e7a137288a8b.
Plan: [markdown-mcp-consent-origin-plan.md](markdown-mcp-consent-origin-plan.md).

## Finding and change
Reviewed latest parent work records, deployed-source revision e7ece39, and the
prior public-check script. That script submitted consent without Origin and could
not detect browser referrer-policy behavior. The older active worktree was behind
main; changes are isolated in /tmp/markdown-mcp-origin-20261030, branch
fix/consent-origin, based on e7ece39. Existing worktree and main notes preserved.

Browser reproduction with real OAuthIngressMiddleware and pinned FastMCP consent
HTML: no-referrer caused native form POST Origin:null and 403 invalid_origin.
Successful GET /consent HTML now uses strict-origin; all other existing OAuth
response policies remain no-referrer. The form's Referer exposes only the public
origin, without transaction path/query. Null/foreign origin rejection, CSRF,
host checks, cookies, no-store and authentication remain unchanged.

Changed auth_service.py, tests/test_oauth.py, ARCHITECTURE.md and generated
code-map.yaml. T-1 through T-4 complete for local reproduction, implementation,
verification and records. Actual user's failing request was not captured; this
is a reproduced browser defect matching the reported error, not historical trace
attribution. Access logging is disabled by design; no log settings were changed.

## Acceptance and evidence
| Criterion | Status | Evidence and limits |
| --- | --- | --- |
| AC-1 | PASS, supplemental browser | Before 403/null; after two 200 submissions with real local Origin and origin-only Referer, using click then keyboard Enter; zero page errors |
| AC-2 | PASS, local OAuth fixtures | Actual SDK consent with valid CSRF gives Cognito 302; null, attacker and ChatGPT origins still rejected; existing CSRF/replay tests pass |
| AC-3 | PASS, local | 45 OAuth/transport tests passed; no-referrer/no-store regression assertions; Ruff and diff checks passed |
| AC-4 | PASS, local scope | Diff reviewed; plan READY/task receipt verified; summary delivery follows this save |
| Native Computer Use | BLOCKED | Repeated app-changed guard prevented actions; user's existing Chrome preserved; not replaced by a claim of native success |
| Deployment/private OTP/upload | NOT RUN | No push, merge or deployment authorized in this follow-up; no live authentication or upload claim |

Commands from /tmp/markdown-mcp-origin-20261030:
- /tmp/markdown-oauth-venv/bin/python -m pytest tests/test_oauth.py tests/test_mcp.py -q: 45 passed in 6.07s; 19 known BaseStore instability warnings.
- /tmp/markdown-oauth-venv/bin/ruff check mcp_services/auth_service.py tests/test_oauth.py: passed.
- git diff --check: passed. Code map regenerated with existing /tmp/generate-mcp-maps.py; only new test definition added.

Supplemental UI evidence and fixture:
.codex/tools/ui-testing-and-debugging/runs/mcp-origin-20261030/ under shared CODEX_HOME.
Final evidence includes synthetic request headers/statuses and blocked native-run
record; local HTTP fixture models browser policy, not real HTTPS/Cognito cookies.
Playwright closed with no open tabs; fixture server exited cleanly. Existing
unrelated softrobots-cognito-browser processes were preserved. No owned native
window opened. No credentials, real tokens, OTP or document content accessed.

## Delivery
Plan READY document/task 71c76857-e273-4cea-a4b8-996e4221a635, submitted SHA256
7cf855bda9ddfcedcd01a2a95f489316aecf045d2dd5efdd3d578d6893932a85.
Canonical project .codex/document-delivery/receipts.json retains revision receipts.
No S3 version was returned. Summary is uploaded immediately after save; check its
exact receipt. Remote task status remains pending; no tracker edit was performed.
Next delivery step is authorized GitHub CI deployment, followed by bounded public
verification and private human OTP/host acceptance. This fix was not deployed.

Source: https://fetch.spec.whatwg.org/#append-a-request-origin-header and the
pinned FastMCP consent template. No origin allowlist expansion was necessary.
