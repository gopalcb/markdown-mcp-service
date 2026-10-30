# Standalone Markdown MCP delivery result

Implementation and ECS deployment complete. OAuth host connection setup remains pending.

Project: /Users/gopalcbala/Desktop/WORK_N_PROJECTS/gb-systems/markdown-mcp-service. Repository: https://github.com/gopalcb/markdown-mcp-service. The independently built container runs as service markdown-mcp-service in the existing softrobots-systems cluster, account 249766001220, ca-central-1. No new cluster was created.

Endpoint: https://ma-b3704d923a424812b95854c699ddeb9e.ecs.ca-central-1.on.aws/mcp/

Deployment run: https://github.com/gopalcb/markdown-mcp-service/actions/runs/41043343195 (success). Deployed source commit: 2de9f229d959c3fb2587277f00d2faf6fe6b079f. Exact ECR digest and public-check responses are in deployment.json. Container startup first denied all MCP requests while Express assigned its HTTPS hostname, then enabled the exact audience-bound configuration using the same tested image.

T-1 through T-4 complete for implementation/public deployment: standalone Docker context, pinned dependencies, versioned S3/index storage snapshot with provenance, independent OIDC delivery role, service/image/runtime stack, tests, docs/maps and publication. Softrobots migration PR47 merged as 106546be5aeea9f8d112f6a3f9faf1930e4a56c4; it removed the superseded app/workflow and retained only a hash-pinned one-time identity bootstrap. Auth bootstrap 41041284776 and identity bootstrap 41042738115 both succeeded. Original dirty checkout and user guide were preserved.

| Acceptance | Result | Evidence |
| --- | --- | --- |
| Standalone clone/build without sibling inputs | PASS | Local image build; GitHub Linux amd64 image build; source snapshot hashes |
| Strict JWT, bounded ingress, nonroot image, storage contract | PASS locally/CI | 36 service/deployment tests; Docker health200/denial503/UID10001; production no anonymous writes |
| Independent role/image/service using existing cluster | PASS deployed | Repository-specific immutable OIDC trust; existing-cluster preflight; requested image/environment revision SUCCESSFUL |
| Public deployed boundary | PASS | One post-success HTTPS check: health200, exact metadata200, MCP401 plus challenge |
| Actual host OAuth, OTP, refresh/revocation and real document save | BLOCKED | Host callback absent; dedicated client OAuthConfigured=false; private authentication not performed |

Additional verification: four bootstrap tests, both standalone templates and bridge template lint, Ruff, and shared Softrobots formatting passed. Earlier existing portal tests: 293 passed. AWS policy validation reported zero findings before the final exact-role ListAttachedRolePolicies read action was added; final templates/tests and real provisioning passed afterward.

Next step: obtain the actual host callback and dispatch deploy.yml with callback_url to activate the dedicated client. Complete private Cognito login and a harmless versioned upload/receipt check. Do not claim authenticated usability or refresh behavior from public health checks. No browser was opened; owned Docker test containers were stopped.
