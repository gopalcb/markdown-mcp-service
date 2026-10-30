"""Bridge ChatGPT OAuth to Cognito without exposing upstream tokens to the client."""

import asyncio
import hashlib
import logging
import time

import boto3
import httpx2
import jwt
from botocore.config import Config
from fastmcp.server.auth import AccessToken, OAuthProxy, TokenVerifier
from key_value.aio.wrappers.encryption import FernetEncryptionWrapper
from mcp.server.auth.provider import AuthorizationCode, RefreshToken, RegistrationError, TokenError
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from mcp_services.auth_service import verify_access_token
from mcp_services.oauth_storage import OAuthStateStore
from mcp_services.validation_service import valid_oauth_redirect

ALLOWED_REDIRECTS = [
    'https://chatgpt.com/connector_platform_oauth_redirect',
    'https://chatgpt.com/connector/oauth/*',
    'http://localhost:*/callback',
    'http://127.0.0.1:*/callback',
]


class CognitoVerifier(TokenVerifier):
    """Retain the service's exact upstream JWT and personal-owner authorization."""

    def __init__(self, config: dict, jwks=None):
        """Set required scopes and a bounded JWKS fetcher, or fixture key source."""
        super().__init__(required_scopes=[config['MCP_WRITE_SCOPE']])
        self.config = config
        self.jwks = jwks or jwt.PyJWKClient(
            config['COGNITO_ISSUER'] + '/.well-known/jwks.json', timeout=5
        )

    async def verify_token(self, token: str) -> AccessToken | None:
        """Verify Cognito before issuing or accepting any proxy authorization."""
        try:
            claims = await asyncio.to_thread(verify_access_token, token, self.config, self.jwks)
            scopes = claims.get('scope', '').split()
            if (
                claims['sub'] != self.config['ALLOWED_SUB']
                or self.config['MCP_WRITE_SCOPE'] not in scopes
            ):
                return None
            return AccessToken(
                token=token,
                client_id=claims['client_id'],
                subject=claims['sub'],
                scopes=scopes,
                expires_at=claims['exp'],
                claims=claims,
            )
        except (jwt.PyJWTError, ValueError, TypeError, OSError, RecursionError):
            return None


class MarkdownOAuthProxy(OAuthProxy):
    """Add cross-task single-use grants and immediate local session revocation."""

    def __init__(self, state_store, **kwargs):
        """Use shared atomic redemption storage alongside the encrypted library store."""
        self.state_store = state_store
        super().__init__(**kwargs)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        """Reject userinfo and ambiguous redirects before applying library DCR policy."""
        if not client_info.redirect_uris or not all(
            valid_oauth_redirect(str(url)) for url in client_info.redirect_uris
        ):
            raise RegistrationError('invalid_redirect_uri', 'Redirect URI not allowed')
        await super().register_client(client_info)

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        """Claim a code atomically after the SDK validates client, redirect and PKCE."""
        if not await self.state_store.claim_once(
            'code', authorization_code.code, authorization_code.expires_at
        ):
            raise TokenError('invalid_grant', 'Authorization code already used')
        code = await self._code_store.get(key=authorization_code.code)
        if not code or not await self._token_validator.verify_token(
            code.idp_tokens['access_token']
        ):
            raise TokenError('invalid_grant', 'Cognito authorization was not accepted')
        return await super().exchange_authorization_code(client, authorization_code)

    async def exchange_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str]
    ) -> OAuthToken:
        """Permit only one use of a rotating proxy refresh token across all tasks."""
        if not await self.state_store.claim_once(
            'refresh', refresh_token.token, refresh_token.expires_at or time.time() + 90 * 86400
        ):
            raise TokenError('invalid_grant', 'Refresh token already used; reconnect')
        payload = self.jwt_issuer.verify_token(refresh_token.token, expected_token_use='refresh')
        mapping = await self._jti_mapping_store.get(key=payload['jti'])
        if mapping is None or await self.session_revoked(mapping.upstream_token_id):
            raise TokenError('invalid_grant', 'Session revoked')
        result = await super().exchange_refresh_token(client, refresh_token, scopes)
        if await self.session_revoked(mapping.upstream_token_id):
            raise TokenError('invalid_grant', 'Session revoked')
        return result

    async def load_access_token(self, token: str) -> AccessToken | None:
        """Keep downstream token/client identity for client-bound revocation checks."""
        try:
            payload = self.jwt_issuer.verify_token(token)
        except Exception:
            return None
        mapping = await self._jti_mapping_store.get(key=payload['jti'])
        if mapping is None or await self.session_revoked(mapping.upstream_token_id):
            return None
        result = await super().load_access_token(token)
        if result is None:
            return None
        payload = self.jwt_issuer.verify_token(token)
        allowed = set(payload.get('scope', '').split())
        return result.model_copy(
            update={
                'token': token,
                'client_id': payload['client_id'],
                'scopes': [scope for scope in result.scopes if scope in allowed],
            }
        )

    async def verify_claims(self, token: str) -> dict:
        """Adapt verified proxy tokens to the existing authenticated ingress contract."""
        result = await self.load_access_token(token)
        if result is None:
            raise jwt.InvalidTokenError('Invalid OAuth token')
        return result.claims | {'scope': ' '.join(result.scopes)}

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        """Invalidate all tokens in the local session and revoke the upstream refresh token."""
        token_use = 'refresh' if isinstance(token, RefreshToken) else 'access'
        payload = self.jwt_issuer.verify_token(token.token, expected_token_use=token_use)
        mapping = await self._jti_mapping_store.get(key=payload['jti'])
        if mapping is None:
            return
        await self._client_storage.put(
            key=mapping.upstream_token_id,
            collection='revoked-sessions',
            value={'revoked': True},
            ttl=90 * 86400,
        )
        upstream = await self._upstream_token_store.get(key=mapping.upstream_token_id)
        await self._upstream_token_store.delete(key=mapping.upstream_token_id)
        await self._jti_mapping_store.delete(key=payload['jti'])
        if isinstance(token, RefreshToken):
            await self._refresh_token_store.delete(
                key=hashlib.sha256(token.token.encode()).hexdigest()
            )
        if upstream and upstream.refresh_token:
            try:
                async with httpx2.AsyncClient(timeout=10) as client:
                    response = await client.post(
                        self._upstream_revocation_endpoint,
                        data={
                            'token': upstream.refresh_token,
                            'client_id': self._upstream_client_id,
                        },
                    )
                    response.raise_for_status()
            except httpx2.HTTPError:
                logging.getLogger(__name__).warning(
                    'Upstream revocation failed; local session revoked'
                )

    async def session_revoked(self, session_id: str) -> bool:
        """Check the shared tombstone so racing refreshes cannot resurrect a revoked session."""
        return bool(await self._client_storage.get(key=session_id, collection='revoked-sessions'))

    async def revoke_public_client(self, request: Request) -> Response:
        """Accept RFC 7009 public clients without the SDK's erroneously required secret field."""
        form = await request.form()
        client_id, token = form.get('client_id'), form.get('token')
        if (
            not isinstance(client_id, str)
            or not isinstance(token, str)
            or not token
            or len(token) > 16384
        ):
            return JSONResponse({'error': 'invalid_request'}, status_code=400)
        client = await self.get_client(client_id)
        if client is None or client.token_endpoint_auth_method != 'none':
            return JSONResponse({'error': 'invalid_client'}, status_code=401)
        loaded = await self.load_refresh_token(client, token)
        if loaded is None:
            loaded = await self.load_access_token(token)
        if loaded is not None and loaded.client_id == client_id:
            await self.revoke_token(loaded)
        return Response(status_code=200, headers={'Cache-Control': 'no-store'})

    def get_routes(self, mcp_path: str | None = None) -> list:
        """Retain maintained OAuth routes and replace only the public-client revocation bug."""
        return [
            Route('/revoke', self.revoke_public_client, methods=['POST'])
            if route.path == '/revoke'
            else route
            for route in super().get_routes(mcp_path)
        ]


def create_oauth(
    config: dict, state_store=None, signing_key: str | None = None, verifier=None
) -> MarkdownOAuthProxy:
    """Build the maintained adapter with a persistent key and encrypted shared state."""
    if state_store is None or signing_key is None:
        options = Config(connect_timeout=5, read_timeout=10, retries={'total_max_attempts': 1})
        session = boto3.Session(region_name='ca-central-1')
        state_store = OAuthStateStore(
            session.client('dynamodb', config=options), config['OAUTH_STATE_TABLE']
        )
        with session.client('secretsmanager', config=options) as secrets:
            signing_key = secrets.get_secret_value(SecretId=config['OAUTH_SECRET_ARN'])[
                'SecretString'
            ]
    if len(signing_key) < 32:
        raise ValueError('OAuth signing key must contain at least 32 random characters')
    encrypted = FernetEncryptionWrapper(
        key_value=state_store,
        source_material=signing_key,
        salt='markdown-mcp-oauth-state-v1',
    )
    domain = config['COGNITO_DOMAIN']
    return MarkdownOAuthProxy(
        state_store=state_store,
        upstream_authorization_endpoint=domain + '/oauth2/authorize',
        upstream_token_endpoint=domain + '/oauth2/token',
        upstream_revocation_endpoint=domain + '/oauth2/revoke',
        upstream_client_id=config['COGNITO_CLIENT_ID'],
        token_endpoint_auth_method='none',
        token_verifier=verifier or CognitoVerifier(config),
        base_url=config['PUBLIC_BASE_URL'],
        issuer_url=config['PUBLIC_BASE_URL'],
        redirect_path='/auth/callback',
        forward_pkce=True,
        extra_authorize_params={'resource': config['MCP_RESOURCE']},
        valid_scopes=[config['MCP_WRITE_SCOPE']],
        allowed_client_redirect_uris=ALLOWED_REDIRECTS,
        client_storage=encrypted,
        jwt_signing_key=signing_key,
        require_authorization_consent=True,
        enable_cimd=False,
        fallback_refresh_token_expiry_seconds=90 * 86400,
    )
