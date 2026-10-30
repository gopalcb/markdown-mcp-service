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
