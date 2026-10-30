# Standalone Markdown MCP service

User revision: create a separate gb-systems project with independent images and deployment, using the existing ECS cluster and a new container/service. Prior PR45 remains merged; IAM bootstrap run41041284776 is in progress; MCP container has not been deployed.

T-1: Extract into markdown-mcp-service with its own repository, build context, pinned dependencies and compatible storage modules. No sibling checkout required.
T-2: Create a dedicated GitHub OIDC deployment identity via the authorized existing Softrobots CI bootstrap, then deploy independently through the new repo. Reuse softrobots-systems cluster; do not create a new cluster. Preserve existing data/user settings.
T-3: Remove superseded MCP app/workflow from Softrobots through a scoped PR, retaining only necessary bootstrap integration/provenance.
T-4: Run local tests, image verification, template/policy validation, publish and deploy. Check completion every two minutes; verify public boundary once after success.

Acceptance: standalone clone builds/tests; identity and image scopes independent; existing cluster reused; final ECS service health/discovery/401 checks pass; real OAuth login remains disabled until actual host callback and private authentication are supplied.

Tradeoff: storage modules are a pinned provenance-tracked snapshot of the already-tested Work Organizer contract, avoiding private cross-repository build dependencies. Updates require explicit contract tests and source comparison. No existing store migration.
