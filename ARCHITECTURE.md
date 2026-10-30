---
file-group: workflow-docs
---
# Markdown MCP architecture

The standalone `markdown-mcp-service` project runs a nonroot Python 3.12 ECS container that exposes stateless Streamable HTTP at `/mcp/`. FastAPI owns health and protected-resource discovery; FastMCP 4.1.4 with MCP 2 owns negotiation, tools and lifespan. Cognito JWT verification requires RS256, issuer, expiry/issued-at, dedicated client, configured subject and write scope. The OAuth adapter stores upstream tokens only in encrypted shared state; ChatGPT receives separate, signed reference tokens.

`app.py` builds the app and registers `save_markdown`. `mcp_services/validation_service.py` validates deployment configuration and the whole batch. `auth_service.py` authenticates and bounds ingress to 3 MB, a 15-second body deadline and a global 60-request burst replenishing at one request/second per task. Uvicorn bounds concurrency at 32 and disables access/proxy-header logging. Ingress and the SDK allow only the configured public hostname and explicit MCP_ORIGIN_HOST (the regional Express hostname); browser Origin remains restricted to the public CloudFront origin. Forwarded Host headers never establish trust. Authentication precedes body parsing; no forwarded header determines identity.

`storage_service.py` calls the existing document-api `services.document_service.store_documents`, vendored as a provenance-tracked source snapshot under `services/`. The image builds entirely from this repository; updates require explicit upstream comparison and contract tests. No bearer-token forwarding. The ECS task role writes only gopalcbala's S3 prefix and index partition. Every upload first records PENDING, stores an encrypted versioned object, then marks READY with the exact S3 version. Incomplete writes retain an inspectable file ID; automatic SDK write retries are disabled. Batch writes are independent and must not be blindly retried.

Keys remain `agent-work/raw-markdown/gopalcbala/<file-group>/<basename>.md`. Same group/name creates another version, and each index row pins its own version. `metadata.relative_path` is descriptive only. The existing reader's 256 KiB per-file limit applies; total content is limited to 2 MB and ten files. Duplicate basenames are rejected even with different relative-path metadata.

`MCP_ENABLED=false` is deny-all bootstrap: only health succeeds; MCP and metadata return 503. Enabled startup requires complete configuration and an exact HTTPS resource ending `/mcp/`. Cloud resources and OAuth clients are managed through this repository's independent GitHub deployment workflow; existing portal clients and accounts are untouched. The service reuses the existing `softrobots-systems` ECS cluster; it never creates a cluster. See DEPLOYMENT.md for permissions, endpoint binding, cost and cleanup.

Fixture tests verify real SDK transport and signed JWTs with disposable keys. They do not prove real Cognito OTP, host callbacks, refresh/revocation or live AWS storage. Those checks require private host login; the upstream callback is fixed and provisioned automatically.

## CloudFront ingress

The dedicated markdown-mcp-service-edge stack sends HTTPS requests to the existing Express service. Managed CachingDisabled and AllViewerExceptHostHeader policies disable caching/request collapsing and preserve Authorization, MCP protocol headers, cookies and query strings while using the Express hostname for origin Host/TLS routing. All seven methods are forwarded; configured error-cache TTLs are zero. Access logging is disabled. The CloudFront URL is canonical for discovery, WWW-Authenticate and exact JWT audience; the original ECS URL is an authenticated origin, not a second advertised OAuth resource.

Deployment creates/updates the edge against the discovered ECS origin before rolling the enabled container configuration. It keeps the existing service and cluster. An IAM bootstrap update grants the independently deployed role only the required edge operations. Public endpoint verification does not replace real host login, resource-bound token exchange or refresh/upload verification.

## Cognito OAuth adapter

`mcp_services/oauth_service.py` configures the maintained OAuthProxy with its own
CloudFront issuer (including its canonical trailing slash), S256 PKCE, DCR and
RFC 9207 issuer responses. The protected resource remains exact `/mcp/`. Cognito
is upstream, with fixed `/auth/callback` and a public dedicated client. The upstream
request omits `resource`: Cognito rejects URL resource binding with the existing
`markdown-api/write` scope. Initial and refreshed Cognito JWTs require strict
signature/issuer/client/subject/scope validation; absent audience is expected, and
a supplied audience must match the MCP resource. Downstream reference tokens remain
bound to the CloudFront resource; raw Cognito tokens are never accepted at MCP. The broker is OAuth,
not an OpenID provider: it advertises only `markdown-api/write`, without userinfo
or ID tokens. CIMD is disabled, avoiding arbitrary client-metadata fetches.

Explicit consent, signed Secure/HttpOnly/SameSite cookies, distinct upstream and
downstream PKCE, state and redirect checks remain provided by FastMCP. DCR accepts
only ChatGPT's documented callback patterns and loopback `/callback` for desktop
hosts; registration alone grants no tool access. OAuth requests have a separate
60-request burst/one-per-second task budget, 64-KiB bodies and a 15-second deadline.
OAuth responses have no-store/no-referrer headers, except successful GET /consent
HTML uses strict-origin so native form submissions retain their Origin without
leaking transaction paths or queries through Referer. Null and foreign origins
remain rejected; consent CSRF and cookie checks remain enforced. Upstream 5xx response details
are replaced by a generic error. Protocol/auth/HTTP library logs are sanitized.

`oauth_storage.py` uses the workload role and a dedicated DynamoDB table with
strong reads, hashed keys, application-encrypted values and TTL. A stable generated
Secrets Manager key derives distinct encryption and JWT-signing keys. State is
shared during rolling deployments; client registrations expire after 90 days and
can re-register. Runtime never provisions resources. Atomic conditional claims
make codes and rotating refresh tokens single-use across tasks. Local revocation
deletes the upstream token set and writes a 90-day tombstone so racing refreshes
cannot restore access; upstream refresh revocation is attempted without exposing
credentials. The adapter replaces the SDK's public-client revocation handler,
which otherwise erroneously requires a secret field. These narrow library hooks
and persistence hooks are version-pinned and covered by integration fixtures.

The deployment role can manage only this table and key. CloudFormation needs
GetSecretValue for resource stabilization; that permission requires CalledVia
CloudFormation. Ordinary CI has no direct secret-value or table-item access.
CloudFormation outputs only the table name and secret ARN. The task role has
exact-table item access and exact-key read access. The existing document IAM
partition and S3 prefix permissions are unchanged.

## Tool consumer metadata

The save_markdown discovery entry explicitly advertises a write that is not
idempotent and uses external storage. Its destructive hint remains conservative
because saving an existing group/name changes the current version at that key.
Annotations describe effects for client policy; they never replace server auth.
Structured results preserve success and per-file READY/INCOMPLETE receipts.
LangChain integration is client-side; see docs/markdown-mcp-langchain.md.

The optional requirements-client lock installs LangChain MCPAdapter in consumer
and CI environments. Real adapter tests use ASGI HTTP, signed fixture identity
and mocked storage; server container dependencies remain independently pinned.
