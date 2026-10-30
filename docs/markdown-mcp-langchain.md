---
file-group: workflow-docs
---
# Connect LangChain to Markdown MCP

Reviewed 2026-10-30 against LangChain MCP documentation and this server's pinned
FastMCP 4.1.4 implementation. LangChain is a consumer of this server; no LangChain
package is needed in the deployed container. Keep client dependencies in a separate
environment. The current documented MCPAdapter API requires langchain[mcp]>=1.4.0
and is beta; pin a tested client version before production use.

## Discover tools using private OAuth

Install the checked-in client lock alongside the server requirements in an isolated
Python 3.12 environment:

```sh
python3.12 -m venv .venv-client
.venv-client/bin/python -m pip install -r requirements.txt -r requirements-client.txt
```

The client lock currently selects LangChain 1.4.7, FastMCP 4.1.4 and MCP 2.3.0.
Regenerate it with `uv pip compile requirements-client.in --python-version 3.12
--python-platform linux --output-file requirements-client.txt`; the server lock
constrains shared dependencies. CI installs both locks and tests MCPAdapter;
the server image installs only requirements.txt.

 This discovery example
uses no model API, creates no document and prints only discovered tool names:

```python
import asyncio

from fastmcp import Client
from fastmcp.client.auth import OAuth
from langchain.mcp import MCPAdapter

URL = 'https://d3cjbof2ij7jy9.cloudfront.net/mcp/'


async def discover_tools() -> None:
    """Discover the Markdown tool after the user privately completes OAuth."""
    oauth = OAuth(
        mcp_url=URL,
        scopes=['markdown-api/write'],
        callback_host='127.0.0.1',
    )
    async with MCPAdapter(Client(URL, auth=oauth)) as adapter:
        tools = await adapter.list_tools()
        print([tool.name for tool in tools])


if __name__ == '__main__':
    asyncio.run(discover_tools())
```

The client registers with the MCP adapter and uses a loopback `/callback`; the
server already permits that redirect shape. Complete consent and Cognito email
OTP privately. Never substitute the upstream Cognito client ID or a raw Cognito
access token: this endpoint accepts its own resource-bound reference tokens.
Keep the trailing slash in `/mcp/`. Do not add browser Origin headers to a normal
server-side client. Persistent token storage, if added by a consumer, needs its
own private credential-storage configuration.

## Writes, receipts and failure handling

`save_markdown` advertises a title and explicit annotations: readOnlyHint=false,
idempotentHint=false, openWorldHint=true, destructiveHint=true. The destructive
hint is conservative because a repeated group/name changes the current object at
that key, even though prior versions are retained. Clients may use these hints
in their own approval policy; they do not grant authorization or enforce approval.
The server supports one owner, gopalcbala, not arbitrary per-user identities.

Preserve the nested `payload` input shown in README. A completed MCP call is not
necessarily a completed upload. Read `structuredContent` (LangChain exposes it
under ToolMessage.artifact['structured_content']) and require `success: true`
and each receipt's READY status/file_id before claiming delivery. The existing
partial-batch result is a normal MCP result with `success: false`, per-file
INCOMPLETE status and reconciliation IDs. Do not rely solely on ToolMessage.status
or isError. Do not retry uncertain writes: each invocation can create another
object version. Reconcile the returned file_id first. Ordinary tool exceptions
and transport failures are distinct from these per-file outcomes.

No new elicitation/resume mechanism or automatic retries are introduced: resuming
an interrupted client call can repeat writes. Connection pooling, per-run tool
catalog discovery, model selection and client approval UI belong in the consumer.

## Evidence and limits

Local real-SDK HTTP tests cover discovery annotations, the nested input schema,
and structured READY/INCOMPLETE receipts. Installed LangChain MCPAdapter also
discovers and calls the tool through authenticated ASGI HTTP fixtures, preserving
annotations and ToolMessage structured receipt artifacts. These tests use synthetic
authentication and mocked storage, with no model call or cloud write. The LangChain client example is based on
the cited API and the pinned OAuth constructor; it has not been executed against
a private authenticated account. LangChain is installed in the isolated client/test environment and CI, not in
the server image. Private login and live uploads remain unverified. The user
subsequently authorized pushing and deploying the server improvements.

Sources:
- https://docs.langchain.com/oss/python/langchain/mcp
- https://docs.langchain.com/oss/python/langchain/mcp/tools
- https://docs.langchain.com/oss/python/langchain/mcp/auth
- https://docs.langchain.com/oss/python/langchain/mcp/connections
