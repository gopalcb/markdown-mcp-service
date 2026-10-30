"""Fail-closed Cognito verification and bounded ASGI ingress without credential logging."""

import asyncio
import inspect
import time

import jwt
from starlette.responses import JSONResponse

MAX_BODY_BYTES = 3_000_000


def verify_access_token(token: str, config: dict, jwks, require_audience: bool = True) -> dict:
    """Verify Cognito identity and client; require audience for direct resource tokens."""
    key = jwks.get_signing_key_from_jwt(token)
    claims = jwt.decode(
        token,
        key.key,
        algorithms=['RS256'],
        issuer=config['COGNITO_ISSUER'],
        audience=config['MCP_RESOURCE'],
        options={
            'require': ['exp', 'iat', 'sub', 'iss'] + (['aud'] if require_audience else []),
            'verify_aud': require_audience,
        },
        leeway=30,
    )

    if not require_audience and 'aud' in claims and claims['aud'] != config['MCP_RESOURCE']:
        raise jwt.InvalidTokenError('Invalid upstream audience')

    if (
        claims.get('token_use') != 'access'
        or claims.get('client_id') != config['COGNITO_CLIENT_ID']
    ):
        raise jwt.InvalidTokenError('Invalid access token binding')

    if not isinstance(claims.get('scope', ''), str):
        raise jwt.InvalidTokenError('Invalid scope claim')

    return claims


class IngressMiddleware:
    """Bound bytes before parsing and rate-limit all MCP requests per container."""

    def __init__(self, app, config: dict, verifier):
        """Keep a bounded global token bucket and injected token verifier."""
        self.app = app
        self.config = config
        self.verifier = verifier
        self.tokens = 60.0
        self.updated = time.monotonic()

    async def __call__(self, scope, receive, send):
        """Authenticate once and replay only a fully bounded request to the SDK."""
        path = scope.get('path', '')

        if scope['type'] != 'http' or not (path == '/mcp' or path.startswith('/mcp/')):
            return await self.app(scope, receive, send)

        if not self.config['enabled']:
            return await JSONResponse({'error': 'not_configured'}, status_code=503)(
                scope, receive, send
            )
        origins = [
            value.decode('latin-1')
            for key, value in scope.get('headers', [])
            if key.lower() == b'origin'
        ]
        if origins and origins != [self.config['PUBLIC_BASE_URL']]:
            return await JSONResponse({'error': 'invalid_origin'}, status_code=403)(
                scope, receive, send
            )
        now = time.monotonic()
        self.tokens = min(60.0, self.tokens + (now - self.updated))
        self.updated = now

        if self.tokens < 1:
            return await JSONResponse(
                {'error': 'rate_limited'}, status_code=429, headers={'Retry-After': '1'}
            )(scope, receive, send)
        self.tokens -= 1
        challenge = f'Bearer resource_metadata="{self.config["PUBLIC_BASE_URL"]}/.well-known/oauth-protected-resource/mcp"'
        authorizations = [
            value for key, value in scope.get('headers', []) if key.lower() == b'authorization'
        ]

        try:
            if len(authorizations) != 1 or len(authorizations[0]) > 16384:
                raise jwt.InvalidTokenError('Missing or oversized authorization')
            scheme, _, token = authorizations[0].decode('latin-1').partition(' ')

            if scheme.lower() != 'bearer' or not token:
                raise jwt.InvalidTokenError('Bearer required')
            if inspect.iscoroutinefunction(self.verifier):
                claims = await self.verifier(token)
            else:
                claims = await asyncio.to_thread(self.verifier, token)
        except (jwt.PyJWTError, ValueError, TypeError, OSError, RecursionError):
            return await JSONResponse(
                {'error': 'invalid_token'}, status_code=401, headers={'WWW-Authenticate': challenge}
            )(scope, receive, send)

        if (
            claims['sub'] != self.config['ALLOWED_SUB']
            or self.config['MCP_WRITE_SCOPE'] not in claims.get('scope', '').split()
        ):
            return await JSONResponse(
                {'error': 'insufficient_scope_or_access'},
                status_code=403,
                headers={'WWW-Authenticate': challenge + ', error="insufficient_scope"'},
            )(scope, receive, send)
        body = bytearray()

        try:
            async with asyncio.timeout(15):
                while True:
                    message = await receive()

                    if message['type'] == 'http.disconnect':
                        return

                    body.extend(message.get('body', b''))

                    if len(body) > MAX_BODY_BYTES:
                        return await JSONResponse({'error': 'body_too_large'}, status_code=413)(
                            scope, receive, send
                        )

                    if not message.get('more_body', False):
                        break
        except TimeoutError:
            return await JSONResponse({'error': 'request_timeout'}, status_code=408)(
                scope, receive, send
            )
        delivered = False

        async def bounded_receive():
            """Deliver the checked bytes once, preserving subsequent disconnect events."""
            nonlocal delivered

            if not delivered:
                delivered = True

                return {'type': 'http.request', 'body': bytes(body), 'more_body': False}

            return await receive()

        await self.app(scope, bounded_receive, send)


class OAuthIngressMiddleware:
    """Bound OAuth inputs and suppress caching of codes, consent and token responses."""

    def __init__(self, app, config: dict):
        """Keep a separate small OAuth request budget and the explicit public/origin hosts."""
        self.app = app
        self.config = config
        self.tokens = 60.0
        self.updated = time.monotonic()

    async def __call__(self, scope, receive, send):
        """Reject oversized or cross-origin OAuth requests before parsing or persistence."""
        path = scope.get('path', '')
        if scope['type'] != 'http' or path not in {
            '/authorize',
            '/token',
            '/register',
            '/revoke',
            '/auth/callback',
            '/consent',
        }:
            return await self.app(scope, receive, send)
        headers = dict(scope.get('headers', []))
        host = headers.get(b'host', b'').decode('latin-1')
        public = self.config.get('PUBLIC_BASE_URL', '')
        allowed_hosts = {public.removeprefix('https://'), self.config.get('MCP_ORIGIN_HOST', '')}
        if not host or host not in allowed_hosts:
            return await JSONResponse({'error': 'invalid_host'}, status_code=421)(
                scope, receive, send
            )
        if headers.get(b'origin') and headers[b'origin'].decode('latin-1') != public:
            return await JSONResponse({'error': 'invalid_origin'}, status_code=403)(
                scope, receive, send
            )
        now = time.monotonic()
        self.tokens = min(60.0, self.tokens + (now - self.updated))
        self.updated = now
        if self.tokens < 1:
            return await JSONResponse(
                {'error': 'rate_limited'}, status_code=429, headers={'Retry-After': '1'}
            )(scope, receive, send)
        self.tokens -= 1
        body = bytearray()
        try:
            async with asyncio.timeout(15):
                while True:
                    message = await receive()
                    if message['type'] == 'http.disconnect':
                        return
                    body.extend(message.get('body', b''))
                    if len(body) > 65536 or len(scope.get('query_string', b'')) > 16384:
                        return await JSONResponse({'error': 'request_too_large'}, status_code=413)(
                            scope, receive, send
                        )
                    if not message.get('more_body', False):
                        break
        except TimeoutError:
            return await JSONResponse({'error': 'request_timeout'}, status_code=408)(
                scope, receive, send
            )
        delivered = False

        async def bounded_receive():
            """Replay the bounded OAuth body once without retaining unbounded input."""
            nonlocal delivered
            if not delivered:
                delivered = True
                return {'type': 'http.request', 'body': bytes(body), 'more_body': False}
            return await receive()

        suppress_error_body = False

        async def private_send(message):
            """Keep authorization query data out of browser caches and referrers."""
            nonlocal suppress_error_body
            if message['type'] == 'http.response.start' and message['status'] >= 500:
                suppress_error_body = True
                await JSONResponse(
                    {'error': 'oauth_unavailable'},
                    status_code=503,
                    headers={'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer'},
                )(scope, receive, send)
                return
            if suppress_error_body:
                return
            if message['type'] == 'http.response.start':
                response_headers = [
                    (k, v)
                    for k, v in message.get('headers', [])
                    if k.lower() not in {b'cache-control', b'referrer-policy'}
                ]
                message = message | {
                    'headers': response_headers
                    + [
                        (b'cache-control', b'no-store'),
                        (b'referrer-policy', b'no-referrer'),
                    ]
                }
            await send(message)

        await self.app(scope, bounded_receive, private_send)
