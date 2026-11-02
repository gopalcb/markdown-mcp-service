---
file-group: workflow-docs
---
# Markdown MCP architecture

## Serverless migration target

`Dockerfile.lambda` adds AWS Lambda Web Adapter 1.1.0 pinned to its multi-architecture
image digest. The existing ASGI app and its stateless JSON MCP transport remain the
application boundary. `aws-infrastructure/serverless.yaml` creates a parallel Lambda
function, retained immutable versions, a `live` alias and a Regional REST API. The
Lambda and integration deadlines are 180 seconds. API Gateway uses its response-
streaming Lambda invocation URI and the adapter emits the required metadata plus
eight-null delimiter; MCP still returns one JSON response. REST streaming avoids
HTTP API v2's fixed 30-second integration limit for slow multi-file writes.

CloudFront keeps its current hostname and uncached request policies. Cutover changes
only the edge origin to the execute-api hostname with `/live` as OriginPath. The
adapter removes that stage prefix when present. Both the public hostname and exact
regional origin Host are allowed; forwarded Host never establishes trust. The
existing OAuth table/key, Cognito client/resource server and document storage stay
in their original stacks. The new role has the same owner-scoped data access and
uses Lambda-only trust. The existing ECR repository grants image retrieval to the
exact Lambda function.

Preparation also creates `markdown-mcp-service-preview` from the same template and
image. Its conditional resources include an isolated CloudFront distribution,
dedicated Cognito app client/branding and a separate Lambda/API. The preview URL
supplies the issuer, resource audience and its exact `/auth/callback`; production
CloudFront/client configuration is never updated during preparation. Both Lambda
functions use the production stack's scoped runtime role. Preview OAuth collections
use the `preview:` prefix and its signing/encryption key is derived separately from
the retained master secret with a fixed HMAC domain. Production uses its original
collection keys and original signing key, preserving existing sessions. Preview
tools still access the owner-scoped document store: acceptance writes must use
unique harmless preview filenames and reconcile their exact receipts.

The REST stage applies an aggregate 60-request burst and one request/second refill;
reserved concurrency defaults to two per function, four across both configurations.
CI checks regional capacity before creating
the reservation and fails rather than reducing the safety bound. Per-process ingress
limits remain defense in depth. Application and API Gateway logs do not record
document bodies or credential/query data.

`scripts/serverless.py` deploys a canary without changing production, validates
recent release-bound acceptance evidence for cutover, saves the old origin/path,
and restores that saved configuration for rollback. `serverless.yml` is the only
migration deployment entry point. The original `deploy.yml` remains the ECS rollback
path. No serverless migration command deletes ECS, CloudFront, Cognito or data.
Acceptance additionally binds the current preview URL, client, Lambda version,
image and distribution. A stale preview or one built from a different image cannot
approve production. Preparation reads and requires the exact production/preview
Lambda ECR pull policy, which must be applied separately through reviewed existing
infrastructure CI; it never updates the production app stack to obtain that policy.

Slow storage can outlast a client or function deadline. Cancelling the async tool
does not stop an already-running storage thread; this is covered by a controlled
fixture test. A disconnected/timed-out call therefore never establishes READY:
reconcile each stable file ID before retrying. The 180-second deadline is a bounded
runtime limit, not a claim that every ten-file batch succeeds in that duration.

The Docker Runtime API test executes the actual pinned adapter, verifies streaming
framing, base64 bodies, repeated query values, literal origin Host, cookie forwarding,
two distinct Secure/HttpOnly response cookies, and private response headers. It uses
synthetic data and does not establish managed API Gateway behavior or private OAuth.

## Existing ECS production path

The standalone `markdown-mcp-service` project runs a nonroot Python 3.12 ECS container that exposes stateless Streamable HTTP at `/mcp/`. FastAPI owns health and protected-resource discovery; FastMCP 4.1.4 with MCP 2 owns negotiation, tools and lifespan. Cognito JWT verification requires RS256, issuer, expiry/issued-at, dedicated client, configured subject and write scope. The OAuth adapter stores upstream tokens only in encrypted shared state; ChatGPT receives separate, signed reference tokens.

`app.py` builds the app and registers `save_markdown`. `mcp_services/validation_service.py` validates deployment configuration and the whole batch. `auth_service.py` authenticates and bounds ingress to 3 MB, a 15-second body deadline and a global 60-request burst replenishing at one request/second per task. Uvicorn bounds concurrency at 32 and disables access/proxy-header logging. Ingress and the SDK allow only the configured public hostname and explicit MCP_ORIGIN_HOST (the regional Express hostname); browser Origin remains restricted to the public CloudFront origin. Forwarded Host headers never establish trust. Authentication precedes body parsing; no forwarded header determines identity.

`storage_service.py` calls `services.document_service.store_documents`, an independently maintained adaptation of the shared document-api compact storage contract. `services/PROVENANCE.md` records its upstream origin and reviewed differences. The image builds entirely from this repository; updates require explicit upstream comparison and contract tests. No bearer-token forwarding. The ECS task role writes only gopalcbala's S3 prefix and index partition. Every upload reads its stable UUID5 index identity, conditionally writes encrypted latest bytes, then publishes six compact fields plus an existing optional task. No S3 version ID is requested or stored. READY requires both acknowledgements. Incomplete writes retain an inspectable stable file ID; automatic SDK write retries are disabled. Batch writes are independent and must not be blindly retried.

Keys remain `agent-work/raw-markdown/gopalcbala/<file-group>/<basename>.md`. Same group/name reuses the same document ID and compact row; the object contains the latest bytes. New objects use `IfNoneMatch`, replacements use the verified object ETag with `IfMatch`, and source bindings prevent cross-project replacement. Legacy raw objects without source-binding or upload-token metadata may be adopted at the same owner/group/name; historical UUID4 records remain untouched. Native unresolved objects and foreign bindings fail closed. Existing creation dates and human task edits survive publication. `metadata.relative_path` is descriptive only. The existing reader's 256 KiB per-file limit applies; total content is limited to 2 MB and ten files. Duplicate basenames are rejected even with different relative-path metadata.

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
because saving an existing group/name replaces the current bytes at that key.
Annotations describe effects for client policy; they never replace server auth.
Structured results preserve success and per-file READY/INCOMPLETE receipts.
LangChain integration is client-side; see docs/markdown-mcp-langchain.md.

The optional requirements-client lock installs LangChain MCPAdapter in consumer
and CI environments. Real adapter tests use ASGI HTTP, signed fixture identity
and mocked storage; server container dependencies remain independently pinned.
