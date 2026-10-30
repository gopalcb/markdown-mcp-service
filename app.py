"""Expose the authenticated stateless Markdown MCP server and public discovery routes."""

import logging
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

from fastapi import FastAPI
from fastmcp import FastMCP
from starlette.responses import JSONResponse

from mcp_services.auth_service import IngressMiddleware, OAuthIngressMiddleware
from mcp_services.oauth_service import create_oauth
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
    for name in ('mcp', 'fastmcp', 'authlib', 'httpx', 'httpx2', 'httpcore', 'httpcore2'):
        logger = logging.getLogger(name)
        handler = logging.StreamHandler()
        handler.addFilter(ProtocolLogFilter())
        logger.handlers = [handler]
        logger.propagate = False
        logger.setLevel(logging.ERROR)


def create_app(config: dict | None = None, verifier=None, oauth=None) -> FastAPI:
    """Construct isolated SDK lifecycle and dependencies for production or fixture tests."""

    configure_protocol_logging()
    config = load_config() if config is None else config
    host = urlsplit(config.get('PUBLIC_BASE_URL', 'https://bootstrap.invalid')).netloc
    if config['enabled'] and verifier is None:
        oauth = oauth or create_oauth(config)
        verifier = oauth.verify_claims
    mcp = FastMCP('Markdown Documents', mask_error_details=True)
    mcp_app = mcp.http_app(
        path='/',
        stateless_http=True,
        json_response=True,
        host_origin_protection=True,
        allowed_hosts=list(dict.fromkeys([host, config.get('MCP_ORIGIN_HOST', host)])),
        allowed_origins=[config.get('PUBLIC_BASE_URL', 'https://bootstrap.invalid')],
    )

    @mcp.tool()
    async def save_markdown(payload: UploadBatch) -> dict:
        """Save Markdown to Work Organizer. Same group/name creates a new S3 version; partial batches are not atomic. Do not retry unverified files blindly."""

        return await save_batch(payload, config)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        """Run the SDK session manager for the lifetime of the ASGI worker."""

        async with mcp_app.lifespan(app):
            yield

    app = FastAPI(
        title='Markdown MCP', lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None
    )

    app.add_middleware(IngressMiddleware, config=config, verifier=verifier)
    app.add_middleware(OAuthIngressMiddleware, config=config)

    @app.get('/health')
    async def health() -> dict:
        """Expose container liveness without cloud calls or credentials."""

        return {'status': 'ok'}

    @app.get('/.well-known/oauth-protected-resource/mcp')
    @app.get('/.well-known/oauth-protected-resource')
    async def protected_resource():
        """Advertise the exact resource and the adapter authorization server."""

        if not config['enabled']:
            return JSONResponse({'error': 'not_configured'}, status_code=503)

        return {
            'resource': config['MCP_RESOURCE'],
            'authorization_servers': [
                str(oauth.issuer_url) if oauth else config['PUBLIC_BASE_URL']
            ],
            'scopes_supported': [config['MCP_WRITE_SCOPE']],
            'bearer_methods_supported': ['header'],
        }

    if oauth is not None:
        app.router.routes.extend(oauth.get_routes('/mcp/'))
    app.state.oauth = oauth
    app.mount('/mcp', mcp_app)

    return app


app = create_app()
