"""Exercise signed JWT boundaries, real SDK transport and production storage contracts."""

import asyncio
import importlib
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import jwt
import pytest
from botocore.exceptions import ClientError, ReadTimeoutError
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]

sys.path.insert(0, str(ROOT))
os.environ['MCP_ENABLED'] = 'false'
app_module = importlib.import_module('app')
from mcp_services.auth_service import verify_access_token
from mcp_services.storage_service import save_batch
from mcp_services.validation_service import UploadBatch, load_config, prepare_payload
from services.document_service import store_documents

CONFIG = {
    'enabled': True,
    'PUBLIC_BASE_URL': 'https://mcp.example.com',
    'COGNITO_ISSUER': 'https://cognito-idp.ca-central-1.amazonaws.com/pool',
    'COGNITO_CLIENT_ID': 'client',
    'MCP_RESOURCE': 'https://mcp.example.com/mcp/',
    'MCP_WRITE_SCOPE': 'markdown-api/write',
    'ALLOWED_SUB': 'owner',
    'S3_BUCKET': 'bucket',
    'USER_FILES_TABLE': 'index',
}
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
JWKS = Mock()
JWKS.get_signing_key_from_jwt.return_value = SimpleNamespace(key=KEY.public_key())


def claims() -> dict:
    """Produce a valid short-lived fixture access token payload."""

    return {
        'iss': CONFIG['COGNITO_ISSUER'],
        'aud': CONFIG['MCP_RESOURCE'],
        'sub': 'owner',
        'client_id': 'client',
        'token_use': 'access',
        'scope': 'markdown-api/write',
        'iat': int(time.time()),
        'exp': int(time.time()) + 300,
    }


def verify(token: str) -> dict:
    """Use fixture keys while running the production JWT validation code."""

    return verify_access_token(token, CONFIG, JWKS)


def headers(overrides: dict | None = None) -> dict:
    """Sign real fixture JWTs, including intentionally invalid claims."""
    payload = claims() | (overrides or {})

    return {
        'Authorization': 'Bearer ' + jwt.encode(payload, KEY, algorithm='RS256'),
        'Accept': 'application/json, text/event-stream',
    }


@pytest.mark.parametrize(
    'changes,status',
    [
        ({'exp': 1}, 401),
        ({'iss': 'wrong'}, 401),
        ({'aud': 'wrong'}, 401),
        ({'token_use': 'id'}, 401),
        ({'client_id': 'wrong'}, 401),
        ({'sub': 'other'}, 403),
        ({'scope': 'openid'}, 403),
        ({'scope': []}, 401),
    ],
)
def test_reject_claims(changes: dict, status: int) -> None:
    """Reject each independent authorization failure before MCP dispatch."""

    with TestClient(
        app_module.create_app(CONFIG, verify), base_url=CONFIG['PUBLIC_BASE_URL']
    ) as client:
        assert client.post('/mcp/', headers=headers(changes), json={}).status_code == status


def test_signature_and_discovery() -> None:
    """Expose discovery but reject missing, malformed and wrongly signed tokens."""

    with TestClient(
        app_module.create_app(CONFIG, verify), base_url=CONFIG['PUBLIC_BASE_URL']
    ) as client:
        assert client.get('/health').json() == {'status': 'ok'}

        for route in (
            '/.well-known/oauth-protected-resource',
            '/.well-known/oauth-protected-resource/mcp',
        ):
            assert client.get(route).json()['resource'] == CONFIG['MCP_RESOURCE']
        response = client.post('/mcp/', json={})
        assert response.status_code == 401
        assert 'resource_metadata=' in response.headers['www-authenticate']
        bad_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

        for token in ('nonsense', jwt.encode(claims(), bad_key, algorithm='RS256')):
            assert (
                client.post(
                    '/mcp/', headers={'Authorization': 'Bearer ' + token}, json={}
                ).status_code
                == 401
            )


def test_sdk_and_upload(monkeypatch) -> None:
    """Initialize real SDK, discover tool schema and invoke its validated storage adapter."""
    storage = importlib.import_module('mcp_services.storage_service')
    writer = Mock(return_value=[{'name': 'example.md', 'status': 'READY', 'file_id': 'fixture'}])

    monkeypatch.setattr(storage, 'write_documents', writer)

    with TestClient(
        app_module.create_app(CONFIG, verify), base_url=CONFIG['PUBLIC_BASE_URL']
    ) as client:
        init = client.post(
            '/mcp/',
            headers=headers(),
            json={
                'jsonrpc': '2.0',
                'id': 1,
                'method': 'initialize',
                'params': {
                    'protocolVersion': '2025-03-26',
                    'capabilities': {},
                    'clientInfo': {'name': 'test', 'version': '1'},
                },
            },
        )
        assert init.status_code == 200
        tools = client.post(
            '/mcp/', headers=headers(), json={'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'}
        )
        assert tools.json()['result']['tools'][0]['name'] == 'save_markdown'
        result = client.post(
            '/mcp/',
            headers=headers(),
            json={
                'jsonrpc': '2.0',
                'id': 3,
                'method': 'tools/call',
                'params': {
                    'name': 'save_markdown',
                    'arguments': {
                        'payload': {'files': [{'name': 'example.md', 'content': '# Fixture'}]}
                    },
                },
            },
        )
        assert result.status_code == 200
        assert not result.json()['result'].get('isError')
        assert writer.call_args.args[0]['username'] == 'gopalcbala'


@pytest.mark.parametrize(
    'files',
    [
        [{'name': '../x.md', 'content': 'x'}],
        [{'name': 'x.txt', 'content': 'x'}],
        [{'name': 'x.md', 'content': 'x', 'metadata': {'relative_path': '../escape'}}],
        [{'name': 'x.md', 'content': 'é' * 140000}],
        [{'name': 'x.md', 'content': 'a'}, {'name': 'x.md', 'content': 'b'}],
        [{'name': f'{i}.md', 'content': 'x' * 250001} for i in range(8)],
    ],
)
def test_invalid_batch(files: list) -> None:
    """Reject invalid batch destinations and UTF-8 sizes before storage."""

    with pytest.raises(ValueError):
        prepare_payload(UploadBatch(files=files))


def test_bounded_body_and_rate_limit() -> None:
    """Enforce byte limit without trusting Content-Length, then bound request rate."""

    with TestClient(
        app_module.create_app(CONFIG, verify), base_url=CONFIG['PUBLIC_BASE_URL']
    ) as client:
        assert (
            client.post(
                '/mcp/', headers=headers(), content=iter([b'x' * 1500001, b'x' * 1500000])
            ).status_code
            == 413
        )
        responses = [client.post('/mcp/', json={}).status_code for _ in range(65)]
        assert 429 in responses


def test_versioned_storage_and_uncertainty() -> None:
    """Reuse actual S3/index coordinator; publish READY only after version acknowledgment."""
    payload = prepare_payload(UploadBatch(files=[{'name': 'x.md', 'content': '# fixture'}]))
    s3, table = Mock(), Mock()
    s3.put_object.return_value = {'VersionId': 'version-1'}
    assert store_documents(payload, s3, table, 'bucket', 'request')[0]['status'] == 'READY'
    assert (
        s3.put_object.call_args.kwargs['Key']
        == 'agent-work/raw-markdown/gopalcbala/learning-chatgpt/x.md'
    )
    assert (
        table.update_item.call_args.kwargs['ExpressionAttributeValues'][':version'] == 'version-1'
    )

    table.reset_mock()
    s3.put_object.side_effect = ClientError({'Error': {'Code': 'AccessDenied'}}, 'PutObject')
    assert store_documents(payload, s3, table, 'bucket', 'request')[0]['status'] == 'INCOMPLETE'

    table.update_item.assert_not_called()
    s3.put_object.side_effect = None
    s3.put_object.return_value = {}
    assert store_documents(payload, s3, table, 'bucket', 'request')[0]['status'] == 'INCOMPLETE'

    table.update_item.assert_not_called()


def test_bootstrap_and_config(monkeypatch) -> None:
    """Deny all MCP/discovery during bootstrap and reject resource slash mismatches."""

    with TestClient(app_module.create_app({'enabled': False})) as client:
        assert client.get('/health').status_code == 200
        assert client.post('/mcp/').status_code == 503
        assert client.get('/.well-known/oauth-protected-resource').status_code == 503

    for name, value in CONFIG.items():
        if name != 'enabled':
            monkeypatch.setenv(name, value)

    monkeypatch.setenv('MCP_ENABLED', 'true')
    assert load_config()['enabled']

    monkeypatch.setenv('MCP_RESOURCE', 'https://mcp.example.com/mcp')

    with pytest.raises(ValueError):
        load_config()


def test_protocol_logs_are_sanitized() -> None:
    """Ensure even library exception messages cannot write caller content to logs."""
    record = app_module.logging.LogRecord(
        'mcp.server',
        40,
        '',
        1,
        'secret %s',
        ('document-body',),
        (ValueError, ValueError('token'), None),
    )
    assert app_module.ProtocolLogFilter().filter(record)
    assert record.getMessage() == 'MCP protocol error'
    assert record.exc_info is None


def test_partial_and_timeout_results(monkeypatch) -> None:
    """Preserve partial uncertainty and do not claim success when a timeout follows a save."""

    storage = importlib.import_module('mcp_services.storage_service')
    payload = UploadBatch(
        files=[{'name': 'a.md', 'content': 'a'}, {'name': 'b.md', 'content': 'b'}]
    )
    s3, table = Mock(), Mock()
    s3.put_object.side_effect = [
        {'VersionId': 'first-version'},
        ReadTimeoutError(endpoint_url='https://s3.invalid'),
    ]
    results = store_documents(prepare_payload(payload), s3, table, 'bucket', 'request')
    assert [row['status'] for row in results] == ['READY', 'INCOMPLETE']
    assert table.update_item.call_count == 1

    monkeypatch.setattr(storage, 'write_documents', Mock(return_value=results))
    assert asyncio.run(save_batch(payload, CONFIG))['success'] is False


def test_sdk_host_and_origin_restrictions() -> None:
    """Reject DNS-rebinding hosts and unapproved browser origins before tool execution."""
    with TestClient(
        app_module.create_app(CONFIG, verify), base_url=CONFIG['PUBLIC_BASE_URL']
    ) as client:
        payload = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'}
        assert (
            client.post(
                '/mcp/', headers=headers() | {'Host': 'attacker.example'}, json=payload
            ).status_code
            == 421
        )
        assert (
            client.post(
                '/mcp/', headers=headers() | {'Origin': 'https://attacker.example'}, json=payload
            ).status_code
            == 403
        )


def test_storage_errors_do_not_expose_aws_details(monkeypatch) -> None:
    """Sanitize errors outside the shared per-file coordinator as uncertain failures."""
    storage = importlib.import_module('mcp_services.storage_service')
    monkeypatch.setattr(
        storage,
        'write_documents',
        Mock(
            side_effect=ClientError(
                {'Error': {'Code': 'Denied', 'Message': 'private-detail'}}, 'Connect'
            )
        ),
    )
    payload = UploadBatch(files=[{'name': 'a.md', 'content': 'x'}])
    with pytest.raises(ValueError, match='Storage not confirmed') as error:
        asyncio.run(save_batch(payload, CONFIG))
    assert 'private-detail' not in str(error.value)


def test_cloudfront_resource_with_explicit_origin_host() -> None:
    """Accept the configured origin Host, retaining edge audience and browser-origin checks."""
    edge = CONFIG | {
        'PUBLIC_BASE_URL': 'https://fixture.cloudfront.net',
        'MCP_RESOURCE': 'https://fixture.cloudfront.net/mcp/',
        'MCP_ORIGIN_HOST': 'origin.ecs.ca-central-1.on.aws',
    }
    verifier = lambda token: verify_access_token(token, edge, JWKS)
    with TestClient(
        app_module.create_app(edge, verifier), base_url='https://' + edge['MCP_ORIGIN_HOST']
    ) as client:
        payload = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'}
        authorized = headers({'aud': edge['MCP_RESOURCE']})
        assert client.post('/mcp/', headers=authorized, json=payload).status_code == 200
        assert client.post('/mcp/', headers=headers(), json=payload).status_code == 401
        assert (
            client.post(
                '/mcp/', headers=authorized | {'Origin': edge['PUBLIC_BASE_URL']}, json=payload
            ).status_code
            == 200
        )
        assert (
            client.post(
                '/mcp/',
                headers=authorized | {'Origin': 'https://' + edge['MCP_ORIGIN_HOST']},
                json=payload,
            ).status_code
            == 403
        )
        assert (
            client.post(
                '/mcp/',
                headers=authorized
                | {'Host': 'attacker.example', 'X-Forwarded-Host': 'fixture.cloudfront.net'},
                json=payload,
            ).status_code
            == 421
        )
        response = client.post('/mcp/', json=payload)
        assert edge['PUBLIC_BASE_URL'] in response.headers['www-authenticate']
        assert (
            client.get('/.well-known/oauth-protected-resource').json()['resource']
            == edge['MCP_RESOURCE']
        )


def test_origin_hostname_config_rejects_urls_ports_and_arbitrary_hosts(monkeypatch) -> None:
    """Only the literal regional ECS hostname can extend the SDK Host allowlist."""
    for name, value in CONFIG.items():
        if name != 'enabled':
            monkeypatch.setenv(name, value)
    monkeypatch.setenv('MCP_ENABLED', 'true')
    for host in (
        'https://origin.ecs.ca-central-1.on.aws',
        'attacker.example',
        'origin.ecs.ca-central-1.on.aws:443',
    ):
        monkeypatch.setenv('MCP_ORIGIN_HOST', host)
        with pytest.raises(ValueError, match='MCP_ORIGIN_HOST'):
            load_config()
    monkeypatch.setenv('MCP_ORIGIN_HOST', 'origin.ecs.ca-central-1.on.aws')
    assert load_config()['MCP_ORIGIN_HOST'] == 'origin.ecs.ca-central-1.on.aws'
