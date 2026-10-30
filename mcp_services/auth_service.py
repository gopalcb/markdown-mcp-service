"""Fail-closed Cognito verification and bounded ASGI ingress without credential logging."""

import asyncio
import time

import jwt
from starlette.responses import JSONResponse

MAX_BODY_BYTES = 3_000_000


def verify_access_token(token: str, config: dict, jwks) -> dict:
    """Verify signature and exact resource/client binding before subject authorization."""
    key = jwks.get_signing_key_from_jwt(token)
    claims = jwt.decode(
        token,
        key.key,
        algorithms=['RS256'],
        issuer=config['COGNITO_ISSUER'],
        audience=config['MCP_RESOURCE'],
        options={'require': ['exp', 'iat', 'sub', 'iss', 'aud']},
        leeway=30,
    )

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
