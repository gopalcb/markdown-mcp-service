---
file-group: workflow-docs
---
# LangChain MCP interoperability review
Work item: LC-MCP-1. Status: Local checks complete; user-authorized push and deployment in progress.

Scope: review the requested LangChain MCP guide, apply compatible discovery
metadata and document client integration. Main-thread direct change, based on
merged/deployed 2d71fe4 in /tmp/markdown-mcp-langchain-20261030, branch
improve/langchain-mcp-contract. Preserve other checkouts and existing OAuth fixes.
The explicit MCP request takes precedence over the previously active sibling project.

Findings: LangChain MCPAdapter is a client layer built on FastMCP, not a required
server runtime. Existing HTTP transport, DCR OAuth and nested input schema fit.
No LangChain dependency, new auth mode, broad origin allowlist, retry behavior,
storage change or model API call is needed. Current langchain[mcp]>=1.4.0 API is beta.

Changes: app.py declares the save_markdown title and explicit write/non-idempotent,
external-world and conservative destructive hints. Existing test_sdk_and_upload
now checks discovery metadata, payload schema and structured READY/INCOMPLETE
receipts. README links docs/markdown-mcp-langchain.md; architecture explains
metadata. Code-map regeneration produced no change because no function definition
or responsibility changed. Partial receipt behavior remains backward compatible.

Acceptance:
- PASS: tools/list exposes title and effect hints through the real SDK.
- PASS: tools/call retains structured success/files for READY and INCOMPLETE.
- PASS: OAuth discovery example uses exact /mcp/ URL, scope, permitted loopback
  redirect and no pasted credentials; documentation states private sign-in limits.
- PASS: /tmp/markdown-oauth-venv/bin/python -m pytest tests/test_mcp.py -q:
  30 passed in 1.31s, from the owned worktree.
- PASS: Ruff on app.py/tests/test_mcp.py and final git diff --check.
- NOT RUN: actual LangChain adapter execution, private OAuth/OTP, live upload,
  deployment and native browser tests. No browser was needed/opened for metadata.

Guide delivery: READY document 60eacf0c-db22-456a-836c-10805a220830, SHA256
20cbc294fbfb2c08b404a5c9ddab2ae7ae1cfe6ecc0a4a7bb27637475106df83.
This work-item revision is delivered immediately after saving. Receipts live in
the canonical project's .codex/document-delivery/receipts.json; no S3 version is
returned. This is a compact local work item, not a new uploaded plan: remote
tracker task creation/status synchronization was not performed.

Sources reviewed 2026-10-30:
https://docs.langchain.com/oss/python/langchain/mcp
https://docs.langchain.com/oss/python/langchain/mcp/tools
https://docs.langchain.com/oss/python/langchain/mcp/auth
https://docs.langchain.com/oss/python/langchain/mcp/connections

## Library installation and deployment follow-up
User explicitly requested installed LangChain/MCP libraries, push and deployment.
Installed /tmp/markdown-mcp-langchain-venv with LangChain 1.4.7, FastMCP 4.1.4 and
MCP 2.3.0. requirements-client.in/txt pin the optional client dependency graph
against server requirements.txt. CI installs both locks, checks imports, and
executes real MCPAdapter discovery/tool calls via ASGI with signed synthetic
authentication and mocked storage. No model API or private credentials are used.
The container retains its existing FastMCP/MCP runtime; LangChain runs in clients
and CI rather than adding an unused server runtime dependency.

67 tests passed (tests and scripts/tests) in 6.50s. Two actual LangChain tests
validate discovery hints and ToolMessage receipt artifacts for READY/INCOMPLETE.
Ruff and git diff --check passed. uv pip check: 100 installed packages compatible.
Known warnings: LangChain MCP beta API and pinned BaseStore instability (20 total).
Code map regenerated. Guide updated with reproducible isolated installation.
Prior statements that LangChain execution was untested are superseded for local
fixtures only; private OAuth/OTP and live uploads remain unverified.
GitHub PR validation/merge and main workflow deployment remain pending.
