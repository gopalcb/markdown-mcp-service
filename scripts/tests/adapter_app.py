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
            }
        ),
        media_type='application/json',
        headers={'Cache-Control': 'no-store', 'Referrer-Policy': 'strict-origin'},
    )
    response.set_cookie('first', 'one', secure=True, httponly=True, samesite='lax')
    response.set_cookie('second', 'two', secure=True, httponly=True, samesite='lax')
    return response
