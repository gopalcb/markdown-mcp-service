---
file-group: workflow-docs
---

# Deploy the standalone Markdown MCP service

This private repository, `gopalcb/markdown-mcp-service`, owns its image, service,
application infrastructure and GitHub deployment workflow. It uses the existing
`softrobots-systems` ECS cluster in account `249766001220`, region `ca-central-1`.
It never provisions a cluster. Existing portal services, Cognito users and document
storage remain unchanged.

## One-time independent deployment identity

The existing Softrobots GitHub workflow `markdown-mcp-bootstrap.yml` provisions
only stack `markdown-mcp-service-delivery` and role
`markdown-mcp-service-github-role`. It runs under the existing CI identity using
the already installed named-policy bootstrap grant. The bridge's managed policy
can manage only the new delivery stack and role; it cannot deploy this application.

The bridge contains a reviewed, hash-pinned copy of `aws-infrastructure/delivery.yaml`.
It needs no cross-repository token or private checkout. Its provenance JSON records
the source repository/path and exact SHA-256. Future deployment-permission changes
require updating this source, the reviewed bridge snapshot and its hash together.
The role's inline policy is identical to `aws-infrastructure/deployer-policy.json`.

The new role trusts only this immutable GitHub main-branch subject:

```text
repo:gopalcb@13314049/markdown-mcp-service@1439586244:ref:refs/heads/main
```

Its trust uses the existing account OIDC provider, requires audience
`sts.amazonaws.com`, and has no local-user trust. The repository already returned
this immutable subject configuration on 2026-10-30; no GitHub settings were changed.
Application deployment does not modify its own identity or a shared managed policy.

After merging the bridge into the original repository, run it once and wait for
successful completion before deploying this service:

```bash
gh workflow run markdown-mcp-bootstrap.yml \
  --repo gopalcb/gb-systems-and-agents --ref main
```

The earlier `portal-auth.yml` infrastructure-only bootstrap completed successfully
before extraction. It need not be rerun for normal MCP deployment.

## Deploy this repository

```bash
gh workflow run deploy.yml --repo gopalcb/markdown-mcp-service --ref main
```

Only the standalone main-branch workflow assumes `markdown-mcp-service-github-role`.
Pull requests run tests and build the container without deploying. Manual deployment
runs tests, builds Linux amd64 once, transfers that tested image, publishes it to
ECR repository `markdown-mcp-service`, and deploys the immutable image digest.
CloudFormation stack `markdown-mcp-service` owns the repository, task/execution
roles, seven-day log group and dedicated OAuth client/scope. ECS Express runs
service `markdown-mcp-service` on the existing cluster.

The script rejects local writes, a different repository/branch/role, a missing or
inactive cluster, and images outside its repository. Neither its IAM policy nor
CloudFormation templates grant or declare cluster creation. Network access uses
existing public default subnets. The task role can write only gopalcbala's Markdown
prefix and DynamoDB partition; the execution role pulls only this image repository
and writes only `/markdown-mcp-service` logs. Neither runtime role can deploy.

On first deployment a health-only revision (`MCP_ENABLED=false`) obtains the actual
Express HTTPS hostname. A second revision of the same tested image binds the exact
`/mcp/` resource audience and enables authenticated MCP. Later deployments reuse
the known hostname. Completion checks occur every 120 seconds, with eight checks
per stage. No log streaming or repeated mutation is used. A successful deployment
must match both the requested image and environment, followed by one bounded
health, metadata and anonymous-denial check. The artifact records endpoint, digest,
client ID and OAuth setup state without tokens.

## Connect OAuth privately

The current owner `gopalcbala` belongs to pool `ca-central-1_VwnNJYce8`. The issuer
is `https://cognito-idp.ca-central-1.amazonaws.com/ca-central-1_VwnNJYce8`; managed
login uses `https://softrobots-usernames-249766001220.auth.ca-central-1.amazoncognito.com`.
Deployment resolves the enabled owner's subject and changes no account or pool
settings. The older guide's family pool does not contain that username.

An omitted callback on first deployment keeps OAuth disabled on the dedicated
client. The container can be deployed safely, but this is not a functioning
ChatGPT connection. Supply the actual callback displayed by the MCP host:

```bash
gh workflow run deploy.yml --repo gopalcb/markdown-mcp-service --ref main \
  -f callback_url='https://ACTUAL-HOST-CALLBACK'
```

An omitted callback on later runs preserves its deployed value. The client allows
only authorization-code OAuth with `openid`, `email`, and `markdown-api/write`.
The host must support the predefined public client, actual Cognito discovery,
PKCE S256 and exact resource audience. Unsupported discovery capabilities are not
fabricated. Complete email OTP privately; never provide tokens through job inputs,
chat or source files.

Public HTTP checks do not verify host registration, PKCE/state, OTP, code exchange,
refresh/revocation or real writes. Before real documents, complete those private
checks and a harmless upload with READY index/S3 version/hash readback. Confirm
same-name version history and do not blindly retry incomplete batches.

## Cost and cleanup

One 0.25-vCPU, 0.5-GiB Fargate task runs continuously: 182.5 vCPU-hours and
365 GiB-hours over 730 hours. Applicable load-balancer, IPv4, ECR, logs, Cognito,
S3 and DynamoDB charges are additional; deployments temporarily overlap tasks.
No NAT gateway or cluster is created. Consult current [Fargate rates](https://aws.amazon.com/fargate/pricing/)
and [load-balancer rates](https://aws.amazon.com/elasticloadbalancing/pricing/).
This single-task configuration is not highly available.

Cleanup is a separate explicitly authorized main-branch CI task using this
repository's deployment identity. After checking active clients, execute:

```bash
aws ecs delete-express-gateway-service --region ca-central-1 \
  --service-arn arn:aws:ecs:ca-central-1:249766001220:service/softrobots-systems/markdown-mcp-service
# After service deletion completes:
aws cloudformation delete-stack --region ca-central-1 --stack-name markdown-mcp-service
```

Existing documents, versions, the index, cluster and portal services are retained.
ECR is retained intentionally and continues accruing storage charges until separately
inventoried and approved for deletion. The independent delivery identity and original
bridge policy require a separate reviewed IAM cleanup; normal service cleanup cannot
remove its own deployment identity.
