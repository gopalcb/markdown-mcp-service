"""Add a disposable transport probe to the deny-all production app for Docker tests."""

import json

from starlette.requests import Request
from starlette.responses import Response

from app import app


@app.post('/adapter-contract')
async def adapter_contract(request: Request) -> Response:
    """Echo synthetic transport values and two cookies without touching AWS storage."""
    response = Response(
        json.dumps(
            {
                'path': request.url.path,
                'query': request.scope['query_string'].decode(),
                'body': (await request.body()).decode(),
                'host': request.headers.get('host'),
                'cookies': request.headers.get('cookie'),
                'authorization': request.headers.get('authorization'),
            }
        ),
        media_type='application/json',
        headers={'Cache-Control': 'no-store', 'Referrer-Policy': 'strict-origin'},
    )
    response.set_cookie('first', 'one', secure=True, httponly=True, samesite='lax')
    response.set_cookie('second', 'two', secure=True, httponly=True, samesite='lax')
    return response


@app.get('/adapter-unauthorized')
async def adapter_unauthorized() -> Response:
    """Return the standards-required dynamic OAuth resource challenge."""
    return Response(
        status_code=401,
        headers={
            'WWW-Authenticate': (
                'Bearer resource_metadata="https://production.cloudfront.net/'
                '.well-known/oauth-protected-resource/mcp"'
            )
        },
    )


@app.get('/adapter-forbidden')
async def adapter_forbidden() -> Response:
    """Return the scope-specific OAuth challenge that must survive the origin."""
    return Response(
        status_code=403,
        headers={
            'WWW-Authenticate': ('Bearer error="insufficient_scope", scope="markdown-api/write"')
        },
    )
