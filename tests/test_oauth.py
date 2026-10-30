"""Exercise the real OAuth adapter with signed Cognito fixtures and encrypted shared state."""

import asyncio
import base64
import copy
import hashlib
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import parse_qs, urlsplit

import httpx2
import jwt
import pytest
from botocore.exceptions import ClientError
from fastapi.testclient import TestClient
from fastmcp.server.auth.oauth_proxy.proxy import AsyncOAuth2Client
from test_mcp import CONFIG, JWKS, KEY, app_module, claims

from mcp_services.oauth_service import CognitoVerifier, create_oauth
from mcp_services.oauth_storage import OAuthStateStore

REDIRECT = 'https://chatgpt.com/connector/oauth/fixture-callback'
SIGNING_KEY = 'fixture-persistent-signing-material-' * 2
VERIFIER = 'fixture-code-verifier-with-more-than-forty-three-characters'
CHALLENGE = (
    base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest()).rstrip(b'=').decode()
)


class FakeDynamo:
    """Model atomic conditional item writes without any network or real credentials."""

    def __init__(self):
        """Share records safely across ASGI threads and adapter instances."""
        self.items = {}
        self.lock = threading.Lock()
        self.reads = []

    def get_item(self, **kwargs) -> dict:
        """Record consistent-read intent and return a detached item snapshot."""
        with self.lock:
            self.reads.append(kwargs)
            key = tuple(kwargs['Key'][name]['S'] for name in ('collection', 'key'))
            item = self.items.get(key)
            return {'Item': copy.deepcopy(item)} if item else {}

    def put_item(self, **kwargs) -> dict:
        """Enforce DynamoDB's conditional create under a lock."""
        with self.lock:
            item = kwargs['Item']
            key = tuple(item[name]['S'] for name in ('collection', 'key'))
            if kwargs.get('ConditionExpression') and key in self.items:
                raise ClientError({'Error': {'Code': 'ConditionalCheckFailedException'}}, 'PutItem')
            self.items[key] = copy.deepcopy(item)
        return {}

    def delete_item(self, **kwargs) -> dict:
        """Return the removed record as DynamoDB ALL_OLD does."""
        with self.lock:
            key = tuple(kwargs['Key'][name]['S'] for name in ('collection', 'key'))
            item = self.items.pop(key, None)
            return {'Attributes': item} if item else {}


@pytest.fixture
def oauth_setup(monkeypatch) -> dict:
    """Build real adapter and fixture upstream endpoints, never making external requests."""
    database = FakeDynamo()
    state = OAuthStateStore(database, 'fixture-oauth-state')
    upstream = {'claims': {}, 'calls': []}

    async def fetch_token(client, url: str, **kwargs) -> dict:
        """Emulate Cognito code exchange while checking dedicated-client PKCE binding."""
        assert url == CONFIG['COGNITO_DOMAIN'] + '/oauth2/token'
        assert client.client_id == CONFIG['COGNITO_CLIENT_ID']
        assert client.token_endpoint_auth_method == 'none'
        assert kwargs['redirect_uri'] == CONFIG['PUBLIC_BASE_URL'] + '/auth/callback'
        assert len(kwargs['code_verifier']) >= 43
        upstream['calls'].append(('code', kwargs))
        return {
            'access_token': jwt.encode(claims() | upstream['claims'], KEY, algorithm='RS256'),
            'refresh_token': 'fixture-private-cognito-refresh',
            'expires_in': 300,
            'token_type': 'Bearer',
            'scope': 'markdown-api/write',
        }

    async def refresh_token(client, url: str, **kwargs) -> dict:
        """Return a fresh audience-bound upstream token and record refresh use."""
        assert url == CONFIG['COGNITO_DOMAIN'] + '/oauth2/token'
        assert kwargs['refresh_token'] == 'fixture-private-cognito-refresh'
        upstream['calls'].append(('refresh', kwargs))
        return {
            'access_token': jwt.encode(claims(), KEY, algorithm='RS256'),
            'refresh_token': 'fixture-private-cognito-refresh',
            'expires_in': 300,
            'token_type': 'Bearer',
            'scope': 'markdown-api/write',
        }

    async def revoke(client, url: str, **kwargs) -> httpx2.Response:
        """Accept only the expected private Cognito refresh-token revocation."""
        assert url == CONFIG['COGNITO_DOMAIN'] + '/oauth2/revoke'
        assert kwargs['data'] == {
            'token': 'fixture-private-cognito-refresh',
            'client_id': CONFIG['COGNITO_CLIENT_ID'],
        }
        upstream['calls'].append(('revoke', kwargs))
        return httpx2.Response(200, request=httpx2.Request('POST', url))

    monkeypatch.setattr(AsyncOAuth2Client, 'fetch_token', fetch_token)
    monkeypatch.setattr(AsyncOAuth2Client, 'refresh_token', refresh_token)
    monkeypatch.setattr(httpx2.AsyncClient, 'post', revoke)
    proxy = create_oauth(CONFIG, state, SIGNING_KEY, CognitoVerifier(CONFIG, JWKS))
    return {'database': database, 'state': state, 'proxy': proxy, 'upstream': upstream}


def register(client: TestClient, redirect: str = REDIRECT) -> dict:
    """Register a public DCR client through the actual SDK endpoint."""
    response = client.post(
        '/register',
        json={
            'client_name': 'OAuth fixture',
            'redirect_uris': [redirect],
            'grant_types': ['authorization_code', 'refresh_token'],
            'response_types': ['code'],
            'token_endpoint_auth_method': 'none',
            'scope': 'markdown-api/write',
        },
    )
    assert response.status_code == 201
    return response.json()


def begin_consent(client: TestClient, registration: dict) -> tuple[str, dict]:
    """Follow authorization to the consent form and recover only fixture hidden fields."""
    response = client.get(
        '/authorize',
        params={
            'response_type': 'code',
            'client_id': registration['client_id'],
            'redirect_uri': REDIRECT,
            'scope': 'markdown-api/write',
            'state': 'fixture-client-state',
            'code_challenge': CHALLENGE,
            'code_challenge_method': 'S256',
            'resource': CONFIG['MCP_RESOURCE'],
        },
        follow_redirects=False,
    )
    assert response.status_code == 302
    consent_url = response.headers['location']
    page = client.get(consent_url)
    assert page.status_code == 200
    fields = dict(re.findall(r'name="(txn_id|csrf_token)" value="([^"]+)"', page.text))
    assert set(fields) == {'txn_id', 'csrf_token'}
    return consent_url, fields


def authorize(client: TestClient, registration: dict) -> tuple[str, dict]:
    """Approve real consent and emulate the upstream browser callback with cookies intact."""
    _, fields = begin_consent(client, registration)
    approved = client.post(
        '/consent', data=fields | {'action': 'approve', 'submit': 'true'}, follow_redirects=False
    )
    assert approved.status_code == 302
    upstream_url = urlsplit(approved.headers['location'])
    assert upstream_url.scheme + '://' + upstream_url.netloc == CONFIG['COGNITO_DOMAIN']
    query = parse_qs(upstream_url.query)
    assert query['redirect_uri'] == [CONFIG['PUBLIC_BASE_URL'] + '/auth/callback']
    assert query['resource'] == [CONFIG['MCP_RESOURCE']]
    assert query['code_challenge_method'] == ['S256']
    assert query['code_challenge'][0] != CHALLENGE
    callback = client.get(
        '/auth/callback',
        params={'code': 'fixture-cognito-code', 'state': query['state'][0]},
        follow_redirects=False,
    )
    assert callback.status_code == 302
    downstream = urlsplit(callback.headers['location'])
    assert downstream.scheme + '://' + downstream.netloc + downstream.path == REDIRECT
    returned = parse_qs(downstream.query)
    assert returned['state'] == ['fixture-client-state']
    assert returned['iss'] == [CONFIG['PUBLIC_BASE_URL'] + '/']
    return returned['code'][0], query


def exchange(
    client: TestClient, registration: dict, code: str, verifier: str = VERIFIER
) -> httpx2.Response:
    """Exchange a downstream code at the real public-client token endpoint."""
    return client.post(
        '/token',
        data={
            'grant_type': 'authorization_code',
            'client_id': registration['client_id'],
            'code': code,
            'redirect_uri': REDIRECT,
            'code_verifier': verifier,
            'resource': CONFIG['MCP_RESOURCE'],
        },
    )


def tool_list(client: TestClient, token: str) -> httpx2.Response:
    """Use the issued reference token on the actual stateless MCP transport."""
    return client.post(
        '/mcp/',
        headers={
            'Authorization': 'Bearer ' + token,
            'Accept': 'application/json, text/event-stream',
        },
        json={'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'},
    )


def test_oauth_exchange_restart_refresh_revoke(oauth_setup) -> None:
    """Run DCR, consent, PKCE, encrypted restart, rotating refresh and session revocation."""
    setup = oauth_setup
    with TestClient(
        app_module.create_app(CONFIG, oauth=setup['proxy']), base_url=CONFIG['PUBLIC_BASE_URL']
    ) as client:
        metadata = client.get('/.well-known/oauth-authorization-server').json()
        assert metadata['issuer'] == CONFIG['PUBLIC_BASE_URL'] + '/'
        assert metadata['code_challenge_methods_supported'] == ['S256']
        assert metadata['registration_endpoint'] == CONFIG['PUBLIC_BASE_URL'] + '/register'
        assert metadata['authorization_response_iss_parameter_supported'] is True
        registration = register(client)
        code, query = authorize(client, registration)
        assert (
            client.get(
                '/auth/callback',
                params={'code': 'fixture-cognito-code', 'state': query['state'][0]},
            ).status_code
            == 400
        )
        assert exchange(client, registration, code, 'wrong-verifier').status_code in (400, 401)
        response = exchange(client, registration, code)
        assert response.status_code == 200
        tokens = response.json()
        assert exchange(client, registration, code).status_code == 401
        assert (
            tool_list(client, tokens['access_token']).json()['result']['tools'][0]['name']
            == 'save_markdown'
        )
        assert tool_list(client, jwt.encode(claims(), KEY, algorithm='RS256')).status_code == 401

    persisted = repr(setup['database'].items)
    assert 'fixture-private-cognito-refresh' not in persisted
    assert 'fixture-cognito-code' not in persisted
    assert tokens['access_token'] not in persisted
    assert all(call['ConsistentRead'] for call in setup['database'].reads)
    proxy = create_oauth(
        CONFIG,
        OAuthStateStore(setup['database'], 'fixture-oauth-state'),
        SIGNING_KEY,
        CognitoVerifier(CONFIG, JWKS),
    )
    with TestClient(
        app_module.create_app(CONFIG, oauth=proxy), base_url=CONFIG['PUBLIC_BASE_URL']
    ) as client:
        assert tool_list(client, tokens['access_token']).status_code == 200
        refreshed = client.post(
            '/token',
            data={
                'grant_type': 'refresh_token',
                'client_id': registration['client_id'],
                'refresh_token': tokens['refresh_token'],
                'scope': 'markdown-api/write',
            },
        )
        assert refreshed.status_code == 200
        fresh = refreshed.json()
        assert fresh['refresh_token'] != tokens['refresh_token']
        assert (
            client.post(
                '/token',
                data={
                    'grant_type': 'refresh_token',
                    'client_id': registration['client_id'],
                    'refresh_token': tokens['refresh_token'],
                },
            ).status_code
            == 401
        )
        assert tool_list(client, fresh['access_token']).status_code == 200
        revoked = client.post(
            '/revoke',
            data={
                'client_id': registration['client_id'],
                'token': fresh['refresh_token'],
                'token_type_hint': 'refresh_token',
            },
        )
        assert revoked.status_code == 200, revoked.text
        assert tool_list(client, fresh['access_token']).status_code == 401
        assert tool_list(client, tokens['access_token']).status_code == 401
    assert [kind for kind, _ in setup['upstream']['calls']] == ['code', 'refresh', 'revoke']


def test_consent_state_redirect_and_csrf_fail_closed(oauth_setup) -> None:
    """Reject malicious redirects, unbound callbacks, wrong state and missing consent CSRF."""
    with TestClient(
        app_module.create_app(CONFIG, oauth=oauth_setup['proxy']),
        base_url=CONFIG['PUBLIC_BASE_URL'],
    ) as client:
        bad = client.post(
            '/register',
            json={
                'redirect_uris': ['https://attacker.example/callback'],
                'token_endpoint_auth_method': 'none',
            },
        )
        assert bad.status_code == 400
        registration = register(client)
        _, fields = begin_consent(client, registration)
        assert (
            client.post(
                '/consent',
                data=fields | {'csrf_token': 'wrong', 'action': 'approve', 'submit': 'true'},
            ).status_code
            == 400
        )
        assert (
            client.get('/auth/callback', params={'code': 'fixture', 'state': 'wrong'}).status_code
            == 400
        )
        assert (
            client.get(
                '/auth/callback', params={'code': 'fixture', 'state': fields['txn_id']}
            ).status_code
            == 403
        )
    assert oauth_setup['upstream']['calls'] == []


@pytest.mark.parametrize(
    'changes',
    [
        {'sub': 'other'},
        {'aud': 'wrong'},
        {'client_id': 'other'},
        {'scope': 'openid'},
        {'token_use': 'id'},
    ],
)
def test_upstream_authorization_failure_cannot_mint_tokens(oauth_setup, changes) -> None:
    """Reject correctly signed Cognito tokens that fail any required authorization binding."""
    oauth_setup['upstream']['claims'] = changes
    with TestClient(
        app_module.create_app(CONFIG, oauth=oauth_setup['proxy']),
        base_url=CONFIG['PUBLIC_BASE_URL'],
    ) as client:
        registration = register(client)
        code, _ = authorize(client, registration)
        response = exchange(client, registration, code)
        assert response.status_code == 401
        assert 'access_token' not in response.json()


def test_concurrent_code_exchange_and_expiry(oauth_setup) -> None:
    """Permit one redemption across two independently created adapters sharing DynamoDB."""
    setup = oauth_setup
    with TestClient(
        app_module.create_app(CONFIG, oauth=setup['proxy']), base_url=CONFIG['PUBLIC_BASE_URL']
    ) as client:
        registration = register(client)
        code, _ = authorize(client, registration)
    proxy = create_oauth(
        CONFIG,
        OAuthStateStore(setup['database'], 'fixture-oauth-state'),
        SIGNING_KEY,
        CognitoVerifier(CONFIG, JWKS),
    )
    with (
        TestClient(
            app_module.create_app(CONFIG, oauth=setup['proxy']), base_url=CONFIG['PUBLIC_BASE_URL']
        ) as first,
        TestClient(
            app_module.create_app(CONFIG, oauth=proxy), base_url=CONFIG['PUBLIC_BASE_URL']
        ) as second,
    ):
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(exchange, client, registration, code) for client in (first, second)
            ]
            statuses = sorted(future.result().status_code for future in futures)
        assert statuses == [200, 401]
    with setup['database'].lock:
        for item in setup['database'].items.values():
            item['ttl'] = {'N': str(int(time.time()) - 1)}
    assert asyncio.run(proxy.get_client(registration['client_id'])) is None


def test_revocation_and_refresh_are_bound_to_registered_client(oauth_setup) -> None:
    """A second public client cannot refresh or invalidate another client's session."""
    with TestClient(
        app_module.create_app(CONFIG, oauth=oauth_setup['proxy']),
        base_url=CONFIG['PUBLIC_BASE_URL'],
    ) as client:
        owner = register(client)
        other = register(client)
        code, _ = authorize(client, owner)
        minted = exchange(client, owner, code)
        assert minted.status_code == 200
        tokens = minted.json()
        wrong_refresh = client.post(
            '/token',
            data={
                'grant_type': 'refresh_token',
                'client_id': other['client_id'],
                'refresh_token': tokens['refresh_token'],
            },
        )
        assert wrong_refresh.status_code in (400, 401)
        assert (
            client.post(
                '/revoke',
                data={
                    'client_id': other['client_id'],
                    'token': tokens['refresh_token'],
                },
            ).status_code
            == 200
        )
        assert tool_list(client, tokens['access_token']).status_code == 200
        assert (
            client.post(
                '/revoke',
                data={
                    'client_id': owner['client_id'],
                    'token': tokens['access_token'],
                },
            ).status_code
            == 200
        )
        assert tool_list(client, tokens['access_token']).status_code == 401
        assert (
            client.post(
                '/token',
                data={
                    'grant_type': 'refresh_token',
                    'client_id': owner['client_id'],
                    'refresh_token': tokens['refresh_token'],
                },
            ).status_code
            == 401
        )


def test_oauth_ingress_bounds_host_origin_and_private_response_headers(oauth_setup) -> None:
    """Reject hostile OAuth inputs before persistence and keep normal responses private."""
    with TestClient(
        app_module.create_app(CONFIG, oauth=oauth_setup['proxy']),
        base_url=CONFIG['PUBLIC_BASE_URL'],
    ) as client:
        assert client.post('/register', content=b'x' * 65537).status_code == 413
        assert (
            client.post('/register', headers={'Host': 'attacker.example'}, json={}).status_code
            == 421
        )
        assert (
            client.post(
                '/register', headers={'Origin': 'https://attacker.example'}, json={}
            ).status_code
            == 403
        )
        assert oauth_setup['database'].items == {}
        registered = client.post(
            '/register',
            json={
                'client_name': 'Private response fixture',
                'redirect_uris': [REDIRECT],
                'grant_types': ['authorization_code', 'refresh_token'],
                'response_types': ['code'],
                'token_endpoint_auth_method': 'none',
                'scope': 'markdown-api/write',
            },
        )
        assert registered.status_code == 201
        for response in (registered, client.get('/consent?txn_id=missing')):
            assert response.headers['cache-control'] == 'no-store'
            assert response.headers['referrer-policy'] == 'no-referrer'


def test_upstream_callback_exception_does_not_expose_sensitive_detail(
    oauth_setup, monkeypatch
) -> None:
    """Replace upstream failure HTML with a generic private error without credentials."""
    sentinel = 'fixture-sensitive-upstream-error-detail'

    async def failed_exchange(client, url: str, **kwargs) -> dict:
        """Raise a sensitive fixture diagnostic at the external token boundary."""
        raise RuntimeError(sentinel)

    monkeypatch.setattr(AsyncOAuth2Client, 'fetch_token', failed_exchange)
    with TestClient(
        app_module.create_app(CONFIG, oauth=oauth_setup['proxy']),
        base_url=CONFIG['PUBLIC_BASE_URL'],
    ) as client:
        registration = register(client)
        _, fields = begin_consent(client, registration)
        approved = client.post(
            '/consent',
            data=fields | {'action': 'approve', 'submit': 'true'},
            follow_redirects=False,
        )
        assert approved.status_code == 302
        state = parse_qs(urlsplit(approved.headers['location']).query)['state'][0]
        failed = client.get('/auth/callback', params={'code': 'fixture-code', 'state': state})
        assert failed.status_code == 503
        assert failed.json() == {'error': 'oauth_unavailable'}
        assert sentinel not in failed.text
        assert failed.headers['cache-control'] == 'no-store'
        assert failed.headers['referrer-policy'] == 'no-referrer'


def test_revoke_during_refresh_cannot_resurrect_session(oauth_setup, monkeypatch) -> None:
    """A shared revocation tombstone defeats an overlapping upstream refresh write."""
    entered, release = threading.Event(), threading.Event()
    original_refresh = AsyncOAuth2Client.refresh_token

    async def paused_refresh(client, url: str, **kwargs) -> dict:
        """Pause the upstream response while another task revokes the session."""
        entered.set()
        assert await asyncio.to_thread(release.wait, 5)
        return await original_refresh(client, url, **kwargs)

    monkeypatch.setattr(AsyncOAuth2Client, 'refresh_token', paused_refresh)
    setup = oauth_setup
    proxy = create_oauth(
        CONFIG,
        OAuthStateStore(setup['database'], 'fixture-oauth-state'),
        SIGNING_KEY,
        CognitoVerifier(CONFIG, JWKS),
    )
    with (
        TestClient(
            app_module.create_app(CONFIG, oauth=setup['proxy']), base_url=CONFIG['PUBLIC_BASE_URL']
        ) as first,
        TestClient(
            app_module.create_app(CONFIG, oauth=proxy), base_url=CONFIG['PUBLIC_BASE_URL']
        ) as second,
    ):
        registration = register(first)
        code, _ = authorize(first, registration)
        tokens = exchange(first, registration, code).json()
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(
                first.post,
                '/token',
                data={
                    'grant_type': 'refresh_token',
                    'client_id': registration['client_id'],
                    'refresh_token': tokens['refresh_token'],
                },
            )
            try:
                assert entered.wait(3)
                assert (
                    second.post(
                        '/revoke',
                        data={
                            'client_id': registration['client_id'],
                            'token': tokens['access_token'],
                        },
                    ).status_code
                    == 200
                )
            finally:
                release.set()
            assert future.result(timeout=5).status_code == 401
        assert tool_list(first, tokens['access_token']).status_code == 401
