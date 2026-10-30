# ChatGPT OAuth repair
Status: In progress

Implemented T-1 through T-4: FastMCP4 adapter, encrypted shared DynamoDB state,
strict upstream Cognito verification, fixed callback provisioning, DCR, atomic
code/refresh redemption, local revocation tombstones and public-client revocation
handler. Existing personal Markdown storage contract retained.

Local evidence: 60 tests passed (31 transport/storage, 12 OAuth, 17 deployment),
Ruff and all three CloudFormation templates passed. Four bootstrap tests passed.
OAuth fixtures exercise actual HTTP routes with synthetic signed Cognito responses,
including concurrent redemption and revoke-during-refresh. They are not real OTP.

The library's BaseStore extension API emits an instability warning; versions are
pinned and persistence/restart behavior is covered by tests. Overrides are limited
to library-required storage hooks, atomic redemption and public-client revocation.

Deployment prerequisite: Softrobots PR53, bootstrap run41056512627, completion
pending. Standalone source branch fix/chatgpt-oauth; service deployment pending.
Plugin candidate 0.2.0 preserves identity/audience and switches provided client to
DCR with OIDC disabled (adapter offers OAuth, not OpenID userinfo). Not published yet.

AC-1 through AC-5: local PASS; live verification pending. AC-6 pending deployment
and plugin readback. AC-7 BLOCKED pending private ChatGPT sign-in/OTP and real upload.

Only material implementation additions to plan: explicit public-client revoke
handler for a verified SDK bug, strong revocation tombstone for concurrent refresh,
and URI parsing before pattern matching to reject userinfo/ambiguous callbacks.

Reference templates permit secret-value reads only through CloudFormation to meet
AWS resource-handler stabilization requirements. Ordinary CI cannot read secrets;
the task reads only the exact key. Deployer policy is 8063 bytes.

Final AWS managed-login documentation check found a separate required OTP setting:
ALLOW_USER_AUTH on the dedicated client. Added it without changing pool policy or
other clients, with a template regression assertion. First adapter deployment
run41057238373 is still in progress; follow-up config deployment is required.
Source: https://docs.aws.amazon.com/cognito/latest/developerguide/authentication-flows-selection-managedlogin.html

Deployment run41057238373 failed and ECS rolled back to its previous healthy task.
Focused startup logs identified TypeError: boto3 SecretsManager client does not
support context-manager protocol. Replaced the with block with try/finally close
and added a production create_app startup test using a stubbed actual boto client.
Queued run41058205857 was cancelled to avoid repeating the startup failure.
The next deployment includes this fix and the already merged USER_AUTH setting.

Run41060622441 completed successfully. The live consent-to-Cognito check exposed
invalid_request: custom scopes requested for resource-binding must be assigned
to the resource being requested. Cognito rejects markdown-api/write with the
CloudFront URL resource. Disabled upstream resource forwarding; accept absent
upstream audience while retaining issuer/signature/client/owner/scope checks and
rejecting mismatched supplied audience. Downstream reference tokens retain MCP
audience binding. Fixtures now model actual audience-less Cognito tokens.
All 61 tests pass; updated correction requires one further deployment.
