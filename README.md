---
file-group: workflow-docs
---
# Markdown MCP Service

Authenticated `save_markdown` uploads to the existing Work Organizer versioned storage and index. Standalone project and repository for independent image builds and GitHub deployment. It uses a dedicated service/container in the existing `softrobots-systems` ECS cluster; no new cluster is provisioned. See [architecture](ARCHITECTURE.md) and [deployment](DEPLOYMENT.md).

Deployed CloudFront MCP endpoint: [https://d3cjbof2ij7jy9.cloudfront.net/mcp/](https://d3cjbof2ij7jy9.cloudfront.net/mcp/). Health, metadata and anonymous rejection are verified. OAuth login remains disabled until the real host callback is registered; authenticated uploads are not yet verified. See [deployment evidence](docs/tasks/2026-10-30-cloudfront/summary.md).

Build from the repository root:

```sh
docker build -f Dockerfile -t markdown-mcp-service:local .
```

Local verification:

```sh
python3.12 -m venv /tmp/markdown-mcp-venv
/tmp/markdown-mcp-venv/bin/pip install -r requirements.txt pytest
/tmp/markdown-mcp-venv/bin/python -m pytest -q tests
```

Local health-only startup (no AWS calls):

```sh
MCP_ENABLED=false PYTHONPATH=. \
  /tmp/markdown-mcp-venv/bin/uvicorn app:app --host 127.0.0.1 --port 8000 --no-access-log
```

Behind CloudFront, also set `MCP_ORIGIN_HOST` to the explicit ECS origin hostname; do not set it from request headers.

For an enabled service supply `PUBLIC_BASE_URL` (HTTPS origin, no trailing slash), `MCP_RESOURCE` (exact origin plus `/mcp/`), `COGNITO_ISSUER`, dedicated `COGNITO_CLIENT_ID`, `MCP_WRITE_SCOPE`, `ALLOWED_SUB`, `S3_BUCKET`, `USER_FILES_TABLE` and the AWS region. Use workload credentials; never set token variables. The exact connection URL includes `/mcp/`.

Tool arguments:

```json
{"payload":{"username":"gopalcbala","file-group":"learning-chatgpt","files":[{"name":"example.md","content":"# Example","metadata":{"project":"chatgpt-web-chat","relative_path":"notes"}}]}}
```

`READY` confirms the object and indexed version; `INCOMPLETE` means inspect that `file_id` before retrying. A successful batch has `success: true`. The default group is learning-chatgpt. Paths do not create subdirectories, and files are limited to 256 KiB UTF-8 for compatibility with portal reads.

Real host login requires its exact callback, PKCE and resource-bound access tokens. The resource server does not implement client registration or an OAuth broker. Discovery compatibility, OTP, refresh and revocation need real host verification. AWS describes resource binding in its [authorization endpoint documentation](https://docs.aws.amazon.com/cognito/latest/developerguide/authorization-endpoint.html).
