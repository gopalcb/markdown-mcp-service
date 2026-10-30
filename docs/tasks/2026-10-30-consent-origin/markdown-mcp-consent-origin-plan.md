---
file-group: workflow-docs
kind: plan
project: Markdown MCP Service
assigned_to: codex
title: Fix browser consent form invalid_origin
---
# Fix browser consent form invalid_origin
Status: Planned. Follow-up to OAuth task 74364279-b4ca-4ec9-835e-e7a137288a8b.

## Evidence and approach
Current main e7ece39 includes the deployed Cognito scope repair. Prior OAuth work
records and /tmp/markdown-oauth-public-check.py show an HTTP-client consent POST
without a browser-generated Origin. Source returns no-referrer on all OAuth
responses and rejects null origins. The Fetch standard's Origin algorithm sets
Origin to null for non-CORS form POSTs under no-referrer.
Source: https://fetch.spec.whatwg.org/#append-a-request-origin-header (2026-10-30).
Access logging is intentionally disabled; no historical Origin capture exists in
these work records. Actual user's request remains unobserved.

T-1: Reproduce native form submission with the actual OAuth ingress middleware
and pinned FastMCP consent HTML in a local fixture; no cloud credentials/state.
T-2: Set strict-origin only for successful GET /consent HTML, retaining no-referrer
for other OAuth responses. Keep null/foreign origin rejection, host checks, CSRF,
cookies, authentication and no-store intact. Worktree /tmp/markdown-mcp-origin-20261030.
T-3: Add regression coverage for browser-compatible consent policy, allowed-origin
consent with valid CSRF, foreign/null rejection and sensitive response privacy;
update architecture and regenerate code map. Run OAuth/transport pytest and Ruff.
T-4: Inspect final diff, record browser evidence separately from API fixtures,
and deliver this plan and final summary through existing scoped uploader.

AC-1: Before fix native form sends null and receives invalid_origin; after fix
same-origin form succeeds with origin-only Referer, without URL query leakage.
AC-2: Actual OAuth fixture progresses to upstream redirect with valid consent;
null/foreign requests and missing CSRF still fail closed.
AC-3: Other OAuth responses retain no-referrer and no-store; targeted tests pass.
AC-4: Final diff and document receipts recorded; private OTP/upload and live fix
remain unverified. No push, merge, deployment or private login in this follow-up.

Browser scope: local pinned consent HTML and real ingress middleware; Chrome desktop,
two clean final submissions, network Origin/Referer/status evidence. This is not
end-to-end Cognito acceptance. Supplemental automation may capture exact headers.
Risk: strict-origin discloses only the already-public origin, never path/query;
limit it to the page that requires a browser form submission. No allowlist expansion.
