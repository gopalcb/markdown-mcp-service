---
file-group: workflow-docs
---
# Markdown MCP architecture

The standalone `markdown-mcp-service` project runs a nonroot Python 3.12 ECS container exposes stateless Streamable HTTP at `/mcp/`. FastAPI owns health and protected-resource discovery; the official MCP v1 SDK owns negotiation, tools and lifespan. Cognito JWT verification requires RS256, issuer, expiry/issued-at, exact resource audience, dedicated client, configured subject and write scope. No refresh tokens are received or stored.

`app.py` builds the app and registers `save_markdown`. `mcp_services/validation_service.py` validates deployment configuration and the whole batch. `auth_service.py` authenticates and bounds ingress to 3 MB, a 15-second body deadline and a global 60-request burst replenishing at one request/second per task. Uvicorn bounds concurrency at 32 and disables access/proxy-header logging. The SDK restricts Host and browser Origin to the configured public origin. Authentication precedes body parsing; no forwarded header determines identity.

`storage_service.py` calls the existing document-api `services.document_service.store_documents`, vendored as a provenance-tracked source snapshot under `services/`. The image builds entirely from this repository; updates require explicit upstream comparison and contract tests. No bearer-token forwarding. The ECS task role writes only gopalcbala's S3 prefix and index partition. Every upload first records PENDING, stores an encrypted versioned object, then marks READY with the exact S3 version. Incomplete writes retain an inspectable file ID; automatic SDK write retries are disabled. Batch writes are independent and must not be blindly retried.

Keys remain `agent-work/raw-markdown/gopalcbala/<file-group>/<basename>.md`. Same group/name creates another version, and each index row pins its own version. `metadata.relative_path` is descriptive only. The existing reader's 256 KiB per-file limit applies; total content is limited to 2 MB and ten files. Duplicate basenames are rejected even with different relative-path metadata.

`MCP_ENABLED=false` is deny-all bootstrap: only health succeeds; MCP and metadata return 503. Enabled startup requires complete configuration and an exact HTTPS resource ending `/mcp/`. Cloud resources and OAuth clients are managed through this repository's independent GitHub deployment workflow; existing portal clients and accounts are untouched. The service reuses the existing `softrobots-systems` ECS cluster; it never creates a cluster. See DEPLOYMENT.md for permissions, endpoint binding, cost and cleanup.

Fixture tests verify real SDK transport and signed JWTs with disposable keys. They do not prove real Cognito OTP, host callbacks, refresh/revocation or live AWS storage. Those checks require the actual host callback and private login.
