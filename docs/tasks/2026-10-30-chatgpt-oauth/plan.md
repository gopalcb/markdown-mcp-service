# Repair ChatGPT OAuth end to end

## Scope and approach
Keep the CloudFront MCP URL, existing ECS cluster/service, Cognito owner/pool/client,
and Markdown storage contract. Add a maintained FastMCP 4.1.4 OAuthProxy in the
existing container: its own issuer, S256 discovery, DCR, consent, PKCE, reference
tokens and refresh; Cognito remains the upstream identity provider. A fixed
CloudFront /auth/callback is the only upstream callback. Preserve signature,
issuer, dedicated-client, owner and scope verification for Cognito tokens. Require
resource audience on downstream tokens; upstream Cognito tokens may omit audience
because the existing custom scope cannot be combined with URL resource binding.
Do not forward upstream tokens to ChatGPT or fabricate Cognito discovery.

Persist OAuth state encrypted in a dedicated TTL DynamoDB table with a stable
Secrets Manager generated signing/encryption secret. Strong reads and atomic
redemption guards prevent cross-task stale reads/code replay during deployments.
Limit registration redirects to documented ChatGPT callbacks and local loopback
callbacks used by the desktop client; require explicit consent. Disable CIMD
unless needed; DCR avoids fetching arbitrary client URLs. Bound auth request bodies
and rates; suppress library logs that might contain credentials.

## Ordered work
T-1: Implement adapter, storage and configuration; migrate transport dependency.
T-2: Provision only owned state/key resources, scoped roles and automatic callback;
update existing independent CI plus its established delivery-role bootstrap.
T-3: Exercise discovery, DCR, consent, callback/state, S256/code exchange, replay,
refresh/revocation and owner/scope/audience failures with synthetic upstream tokens.
T-4: Update architecture, deployment docs, generated maps; inspect final diff and
run pytest, Ruff, CloudFormation checks. Publish/merge without bypassing protections.
T-5: Deploy through GitHub once per changed stage; completion checks every 120s.
Make bounded final public checks including a real redirect to Cognito login.
T-6: Change existing private plugin to DCR, read back saved files, and distinguish
public/fixture success from private ChatGPT OTP and real upload verification.

## Acceptance and verification
AC-1: Published discovery advertises S256 and correct CloudFront issuer/resource.
AC-2: OAuth code grant enabled on dedicated Cognito client with fixed callback.
AC-3: DCR and consent produce PKCE upstream redirect; replay, wrong state/PKCE,
wrong audience/subject/client/scope and anonymous tool requests fail closed.
AC-4: Encrypted shared state survives process replacement; secrets absent from
source, deployment output, logs and ChatGPT tokens; role grants are scoped.
AC-5: Existing Markdown validation/storage contract and ingress restrictions pass.
AC-6: Existing ECS cluster/service updated successfully; plugin saved with DCR.
AC-7: Actual ChatGPT private sign-in and upload verified only after human OTP;
otherwise explicitly BLOCKED, never inferred from fixtures or public checks.

## Research and risks
Checked 2026-10-30: https://gofastmcp.com/servers/auth/oauth-proxy,
https://developers.openai.com/plugins/build/auth,
https://developers.openai.com/plugins/build/plugins,
https://docs.aws.amazon.com/cognito/latest/developerguide/authorization-endpoint.html.
FastMCP4 requires MCP2 transport migration. Library configuration/source and tests
must establish exact resource, redirect and storage behavior. DynamoDB state and
Secrets Manager add small usage-based charges; no new cluster/cache service.
User privately completes email OTP. No account or pool-wide login policy changes.
