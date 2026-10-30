# CloudFront endpoint delivery

Complete for implementation and public deployment. The canonical MCP endpoint is https://d3cjbof2ij7jy9.cloudfront.net/mcp/. Distribution E3BS4AEVAMTQSF forwards to the unchanged Express origin; the existing markdown-mcp-service ECS service and softrobots-systems cluster are reused. No new cluster or ECS service was created.

Standalone PR1 and bootstrap PR50 merged after checks passed. Role update run 41047091682 succeeded. Independent deployment run 41047704121 succeeded, including exact image/environment revision verification and endpoint checks. Exact image digest, origin, distribution and final public responses are recorded in deployment.json.

Forty-one local and CI tests passed; Ruff, all three app templates and the bridge template passed. Four bridge tests passed. IAM Access Analyzer returned zero findings; nine scoped-permission simulations passed. The updated deployment role inline policy is 6,636 bytes.

One final post-success public HTTPS check confirmed health 200, metadata 200 with the exact CloudFront resource and no Age header, and POST /mcp/ returning 401 with a CloudFront metadata challenge. The metadata route reported a CloudFront cache miss. These are public transport checks, not authenticated upload evidence.

All implementation/public-deployment acceptance criteria pass. Private host OAuth, OTP, refresh/revocation and real document writes remain BLOCKED on the actual host callback and private sign-in. The dedicated OAuth client remains disabled. Fixture tests prove explicit origin Host acceptance, old audience rejection, arbitrary Host/browser Origin rejection and no forwarded-Host trust.

The local project checkout is synchronized to merged code. Existing storage, users and portal resources were preserved. Deployment ran only through GitHub; completion was checked at two-minute-or-longer intervals, without intermediate log inspection or retriggering.
