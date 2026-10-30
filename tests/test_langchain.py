"""Verify the optional LangChain client against the authenticated ASGI MCP boundary."""

import asyncio
import importlib
from unittest.mock import Mock

import httpx2
import pytest
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from test_mcp import CONFIG, app_module, headers, verify

MCPAdapter = pytest.importorskip('langchain.mcp').MCPAdapter


@pytest.mark.parametrize('status', ['READY', 'INCOMPLETE'])
def test_langchain_discovery_and_receipts(monkeypatch, status: str) -> None:
    """Adapt real HTTP tools and retain per-file results without a model or cloud write."""
    receipts = [{'name': 'client-fixture.md', 'status': status, 'file_id': 'fixture'}]
    writer = Mock(return_value=receipts)
    monkeypatch.setattr(importlib.import_module('mcp_services.storage_service'), 'write_documents', writer)
    application = app_module.create_app(CONFIG, verify)

    def client_factory(**kwargs) -> httpx2.AsyncClient:
        """Keep every client request inside the fixture ASGI application."""
        return httpx2.AsyncClient(transport=httpx2.ASGITransport(app=application), **kwargs)

    async def exercise_adapter() -> None:
        """Discover and call the actual tool through LangChain's MCP adaptation."""
        transport = StreamableHttpTransport(
            CONFIG['MCP_RESOURCE'],
            headers={'Authorization': headers()['Authorization']},
            httpx_client_factory=client_factory,
        )
        async with application.router.lifespan_context(application):
            async with MCPAdapter(Client(transport)) as adapter:
                tools = await adapter.list_tools()
                tool = next(tool for tool in tools if tool.name == 'save_markdown')
                annotations = tool.metadata['mcp']['tool']['annotations']
                assert annotations['read_only_hint'] is False
                assert annotations['idempotent_hint'] is False
                result = await tool.ainvoke({
                    'name': tool.name,
                    'type': 'tool_call',
                    'id': 'fixture-call',
                    'args': {'payload': {'files': [{
                        'name': 'client-fixture.md', 'content': '# Fixture',
                    }]}},
                })
                assert result.artifact['structured_content'] == {
                    'success': status == 'READY', 'files': receipts,
                }
                assert result.status == 'success'
        writer.assert_called_once()

    asyncio.run(exercise_adapter())
