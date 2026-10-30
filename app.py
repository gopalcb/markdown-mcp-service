"""Expose the authenticated stateless Markdown MCP server and public discovery routes."""

import logging
from contextlib import asynccontextmanager
from functools import partial
from urllib.parse import urlsplit

import jwt
from fastapi import FastAPI
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import JSONResponse

from mcp_services.auth_service import IngressMiddleware, verify_access_token
from mcp_services.storage_service import save_batch
from mcp_services.validation_service import UploadBatch, load_config


class ProtocolLogFilter(logging.Filter):
    """Keep SDK errors observable without logging parsed document or token values."""

    def filter(self, record: logging.LogRecord) -> bool:
        """Replace library diagnostic strings and tracebacks with a safe event category."""
        record.msg = 'MCP protocol error'
        record.args = ()
        record.exc_info = None
        record.exc_text = None
        record.stack_info = None

        return True


def configure_protocol_logging() -> None:
    """Use a sanitized handler for protocol-library errors; storage logs are separate."""
    logger = logging.getLogger('mcp')
    handler = logging.StreamHandler()

    handler.addFilter(ProtocolLogFilter())
    logger.handlers = [handler]
    logger.propagate = False

    logger.setLevel(logging.ERROR)


def create_app(config: dict | None = None, verifier=None) -> FastAPI:
    """Construct isolated SDK lifecycle and dependencies for production or fixture tests."""

    configure_protocol_logging()
    config = load_config() if config is None else config
    host = urlsplit(config.get('PUBLIC_BASE_URL', 'https://bootstrap.invalid')).netloc
    mcp = FastMCP(
        'Markdown Documents',
        stateless_http=True,
        json_response=True,
        streamable_http_path='/',
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=list(dict.fromkeys([host, config.get('MCP_ORIGIN_HOST', host)])),
            allowed_origins=[config.get('PUBLIC_BASE_URL', 'https://bootstrap.invalid')],
        ),
    )

    @mcp.tool()
    async def save_markdown(payload: UploadBatch) -> dict:
        """Save Markdown to Work Organizer. Same group/name creates a new S3 version; partial batches are not atomic. Do not retry unverified files blindly."""

        return await save_batch(payload, config)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        """Run the SDK session manager for the lifetime of the ASGI worker."""

        async with mcp.session_manager.run():
            yield

    app = FastAPI(
        title='Markdown MCP', lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None
    )

    if verifier is None and config['enabled']:
        jwks = jwt.PyJWKClient(config['COGNITO_ISSUER'] + '/.well-known/jwks.json', timeout=5)
        verifier = partial(verify_access_token, config=config, jwks=jwks)

    app.add_middleware(IngressMiddleware, config=config, verifier=verifier)

    @app.get('/health')
    async def health() -> dict:
        """Expose container liveness without cloud calls or credentials."""

        return {'status': 'ok'}

    @app.get('/.well-known/oauth-protected-resource/mcp')
    @app.get('/.well-known/oauth-protected-resource')
    async def protected_resource():
        """Advertise only actual configured resource and Cognito authorization server."""

        if not config['enabled']:
            return JSONResponse({'error': 'not_configured'}, status_code=503)

        return {
            'resource': config['MCP_RESOURCE'],
            'authorization_servers': [config['COGNITO_ISSUER']],
            'scopes_supported': [config['MCP_WRITE_SCOPE']],
            'bearer_methods_supported': ['header'],
        }

    app.mount('/mcp', mcp.streamable_http_app())

    return app


app = create_app()
