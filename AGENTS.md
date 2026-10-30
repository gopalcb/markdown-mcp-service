# Markdown MCP Service

Standalone Python project for Cognito-authenticated Markdown MCP uploads. Follow the shared GB Systems instructions through CODEX_HOME. Read ARCHITECTURE.md, code-map.yaml and DEPLOYMENT.md before relevant changes; apply python-development.

Deployment uses GitHub CI/CD only. Reuse the existing softrobots-systems ECS cluster in ca-central-1; never provision a new cluster for this service. The project owns its container image, ECS service, task identities and deployment workflow. Preserve existing portal resources, Cognito users and storage data. Never log credentials, tokens or document bodies.

Run focused pytest, Ruff and CloudFormation checks. Keep independent repository builds free of sibling-checkout dependencies. Shared storage-contract provenance is recorded under services/PROVENANCE.md. Private OTP and host callback tests must remain clearly separate from local fixture and public endpoint verification.
