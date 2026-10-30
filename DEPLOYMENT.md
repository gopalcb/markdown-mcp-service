---
file-group: workflow-docs
---

# Deploy the standalone Markdown MCP service

This private repository, `gopalcb/markdown-mcp-service`, owns its image, service,
application infrastructure, dedicated CloudFront endpoint and GitHub deployment workflow. It uses the existing
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
before extraction. It need not be rerun for normal MCP deployment. For this
CloudFront addition, first merge and run the updated identity bridge once so the
standalone role receives the reviewed edge permissions, then deploy this repository.

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

The public MCP endpoint is `https://<dedicated-distribution>.cloudfront.net/mcp/`.
Stack `markdown-mcp-service-edge` owns this project's separate distribution and
uses the existing Express HTTPS hostname as its origin. It does not change the
Softrobots portal distribution. On a fresh installation a health-only ECS revision
first obtains its Express origin; the edge is then provisioned before the enabled
revision binds `PUBLIC_BASE_URL` and the exact `/mcp/` resource audience to CloudFront.
An existing ECS service and cluster are reused. `MCP_ORIGIN_HOST` allows the literal
verified Express Host used on the CloudFront origin request; it does not trust
forwarded host headers or bypass Cognito authentication. Direct origin requests
still require the CloudFront-bound access token.

CloudFront redirects viewer HTTP to HTTPS and uses HTTPS with TLS 1.2 to the origin.
The default CloudFront certificate supplies the public hostname; AWS controls its
viewer security policy, so this configuration does not claim a custom viewer
minimum TLS version. All seven HTTP methods are forwarded. The managed
`CachingDisabled` policy gives zero minimum/default/maximum TTL. The managed
`AllViewerExceptHostHeader` policy forwards Authorization, MCP protocol headers,
cookies and query strings while replacing Host with the Express hostname. Supported
custom error responses have zero error-cache TTL without changing status codes.
Standard and real-time request logging are not configured. Authentication and the
application's body/rate limits remain enforced at the origin.

Completion checks run every 120 seconds. A successful ECS deployment must match
both the requested image and environment, followed by one bounded check of the
CloudFront health, metadata and anonymous-denial boundary. The deployment artifact
records the public endpoint, distribution ID, origin and image digest. These public
checks do not establish an authenticated upload or a private OAuth connection.

### Scoped edge permissions

The deployment role can manage only the existing application stack and exact
`markdown-mcp-service-edge` stack. CloudFront creation requires
`Project=markdown-mcp-service-edge` in the request. Reads, configuration updates
and deletion require that tag on the distribution. Creating/tagging distributions
must occur through CloudFormation's forwarded caller credentials, preventing
ordinary direct API calls from tagging another distribution to gain ownership.
Removing the ownership tag is not allowed. The distribution and its stack both
use the same Project value.

CloudFront creation requires IAM resource `*`; subsequent operations use this
account's distribution ARN pattern plus the ownership condition. The role does
not gain access to the existing portal distribution. No managed cache-policy
creation or editing permissions are required. The updated role policy remains
below IAM's 10,240-byte aggregate inline limit. Ordinary application deployment
cannot modify this role; the original identity bridge applies reviewed changes.

Official references checked 2026-10-30: [managed cache policies](https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/using-managed-cache-policies.html),
[managed origin request policies](https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/using-managed-origin-request-policies.html),
[CloudFront IAM actions and conditions](https://docs.aws.amazon.com/service-authorization/latest/reference/list_cloudfront.html),
and [default CloudFront certificate behavior](https://docs.aws.amazon.com/AWSCloudFormation/latest/TemplateReference/aws-properties-cloudfront-distribution-viewercertificate.html).

## Connect OAuth privately

The owner `gopalcbala` remains in pool `ca-central-1_VwnNJYce8`; no user or
pool-wide authentication settings change. The dedicated upstream client remains
`4b7pjdn3vjsrb0rbadrarco4sl`. The deployment reads the owned edge stack output and
automatically enables its code grant, allowed scopes and fixed `/auth/callback`.
No host callback input is needed. On a fresh installation, the first revision is
health-only until the edge exists and the callback is configured.

Run the existing manual workflow on main:

```bash
gh workflow run deploy.yml --repo gopalcb/markdown-mcp-service --ref main
```

Configure the existing ChatGPT plugin for OAuth DCR, `markdown-api/write`, and
`https://d3cjbof2ij7jy9.cloudfront.net/mcp/`. Remove the provided Cognito client ID
from the host configuration. The adapter registers downstream clients and uses
the Cognito client privately for upstream code exchange. Its authorization-server
metadata is `/.well-known/oauth-authorization-server`; the issuer is the CloudFront
origin plus `/`. Never replace Cognito's discovery with fabricated capabilities.

The adapter maintains explicit consent and separate upstream/downstream PKCE.
Follow the host-initiated authorization flow, approve service access and complete
email sign-in privately. No password, OTP, tokens or signing key belongs in chat,
source, job inputs or logs. Saved plugin configuration does not prove a login.

State lives in retained `markdown-mcp-service-oauth`, encrypted by a generated key
in retained `markdown-mcp-service-oauth-key`. Do not rotate/delete this key casually:
existing sessions and encrypted registrations would become unreadable. Deployment
artifacts contain only its ARN. Runtime retrieves it with the task role. The
CloudFormation-only deployment-role GetSecretValue permission exists for resource
handler stabilization, not ordinary CI reads. Cleanup of retained authentication
resources requires separate inventory/authorization.

Tests exercise signed synthetic Cognito tokens through real HTTP OAuth routes,
restart persistence, replay/CSRF, refresh and revocation. Public smoke checks cover
health, discovery, anonymous rejection and reaching real Cognito login. They do
not prove private ChatGPT sign-in, OTP delivery or a real Markdown write. Complete
private acceptance and a harmless upload with READY/version readback before using
real documents; do not blindly retry incomplete uploads.

## Cost and cleanup

One 0.25-vCPU, 0.5-GiB Fargate task runs continuously: 182.5 vCPU-hours and
365 GiB-hours over 730 hours. Applicable CloudFront requests/data transfer,
load-balancer, IPv4, ECR, logs, Cognito, S3, on-demand OAuth DynamoDB state and Secrets Manager charges are additional; deployments temporarily overlap tasks.
No NAT gateway or cluster is created. Consult current [Fargate rates](https://aws.amazon.com/fargate/pricing/)
[CloudFront rates](https://aws.amazon.com/cloudfront/pricing/), and
[load-balancer rates](https://aws.amazon.com/elasticloadbalancing/pricing/).
This single-task configuration is not highly available.

Cleanup is a separate explicitly authorized main-branch CI task using this
repository's deployment identity. After checking active clients, execute:

```bash
# Delete the dedicated edge and wait for completion before removing its origin.
aws cloudformation delete-stack --region ca-central-1 --stack-name markdown-mcp-service-edge
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
