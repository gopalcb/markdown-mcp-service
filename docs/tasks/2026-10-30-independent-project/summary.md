# Standalone project status

In progress. Standalone application, Docker build context, storage snapshot/provenance and local repository are complete at /Users/gopalcbala/Desktop/WORK_N_PROJECTS/gb-systems/markdown-mcp-service. Private remote: https://github.com/gopalcb/markdown-mcp-service.

Thirty-six local service/storage/deployment tests pass; standalone Docker build and live local container health200/deny-all503/nonroot10001 checks pass. Storage snapshot hashes protect explicit source provenance. No sibling build inputs remain.

Existing auth IAM bootstrap run41041284776 completed successfully. No MCP container deployment has occurred. The implemented bootstrap-only Softrobots workflow will create the new main-branch OIDC deployment identity; the standalone workflow will provision its own image/runtime resources and deploy markdown-mcp-service into the existing softrobots-systems ECS cluster. No new cluster is planned or permitted by its policy.

The original Softrobots application/workflow are being removed in isolated migration worktree /tmp/softrobots-extract-mcp-20261030. Existing user checkout remains untouched. Actual host callback is still absent, so the dedicated OAuth client will remain disabled; private OTP, refresh/revocation and real upload verification remain unrun.
