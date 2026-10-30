# CloudFront endpoint for Markdown MCP

Use a dedicated CloudFront distribution owned by the standalone service. Preserve the existing ECS cluster/service and storage, and retain private OAuth configuration requirements. Branch agents/root/cloudfront-20261030 starts at97434ae; isolated Softrobots bridge update starts ate9ac01b.

T-1: Add an HTTPS-only uncached distribution in markdown-mcp-service-edge, using managed CachingDisabled and AllViewerExceptHostHeader policies, all HTTP methods, and zero error-cache TTL. Forward authorization/protocol headers, query strings and cookies without forwarding viewer Host to the Express origin.
T-2: Provision edge with the discovered Express origin, set PUBLIC_BASE_URL and exact MCP_RESOURCE to CloudFront, and allow only the explicit configured Express hostname alongside the public hostname in SDK Host checks. Do not trust forwarded Host headers. Keep browser Origin bound to public CloudFront.
T-3: Update scoped delivery IAM and hash-pinned bootstrap template through reviewed GitHub changes. Deploy role update first, then standalone app/edge once; monitor completion every two minutes.
T-4: Verify tests/template/format, publish, and make one final CloudFront health/metadata/unauthorized check. Save endpoint and deployment evidence.

Acceptance: CloudFront is canonical in metadata/challenges and token audience; actual origin Host is accepted with a valid fixture token while arbitrary hosts/origins and old audiences fail; caching is disabled and authorization forwarding preserved; existing ECS service/cluster reused; public deployed checks pass. Real OTP/refresh/upload remain blocked on actual host callback/private login.

AWS documentation checked 2026-10-30: managed origin request policy AllViewerExceptHostHeader preserves viewer headers except Host, and CachingDisabled prevents caching/request collapsing. The app must explicitly accept the origin hostname because CloudFront sends that Host to the origin.
