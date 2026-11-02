"""Exercise the pinned adapter binary using a local disposable Lambda Runtime API.

Run with MCP_LAMBDA_TEST_IMAGE set to a built Dockerfile.lambda image. This proves
the real adapter envelope and HTTP mapping locally, not managed AWS integration.
"""

import base64
import json
import os
import queue
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs
from uuid import uuid4

import pytest


class RuntimeHandler(BaseHTTPRequestHandler):
    """Serve synthetic REST proxy requests and capture streaming Runtime API replies."""

    def log_message(self, format: str, *args) -> None:
        """Keep request paths and synthetic payloads out of test output."""

    def do_GET(self) -> None:
        """Deliver queued invocations with the mandatory Runtime API context headers."""
        if self.path.endswith('/runtime/invocation/next'):
            try:
                event = self.server.events.get(timeout=30)
            except queue.Empty:
                self.send_error(503)
                return
            body = json.dumps(event).encode()
            self.send_response(200)
            self.send_header('Lambda-Runtime-Aws-Request-Id', str(uuid4()))
            self.send_header('Lambda-Runtime-Deadline-Ms', str(int(time.time() * 1000) + 180000))
            self.send_header(
                'Lambda-Runtime-Invoked-Function-Arn',
                'arn:aws:lambda:ca-central-1:249766001220:function:markdown-mcp-service:live',
            )
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_error(404)

    def do_POST(self) -> None:
        """Decode chunked streaming bytes without interpreting application content."""
        if self.path.endswith('/extension/register'):
            self.rfile.read(int(self.headers.get('Content-Length', '0')))
            body = b'{"functionName":"markdown-mcp-service","functionVersion":"1","handler":"app"}'
            self.send_response(200)
            self.send_header('Lambda-Extension-Identifier', 'fixture-extension')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.headers.get('Transfer-Encoding', '').lower() == 'chunked':
            chunks = []
            while True:
                size = int(self.rfile.readline().split(b';')[0], 16)
                if size == 0:
                    while self.rfile.readline().strip():
                        pass
                    break
                chunks.append(self.rfile.read(size))
                assert self.rfile.read(2) == b'\r\n'
            body = b''.join(chunks)
        else:
            body = self.rfile.read(int(self.headers.get('Content-Length', '0')))
        self.server.responses.put(
            (self.path, {key.lower(): value for key, value in self.headers.items()}, body)
        )
        self.send_response(202)
        self.send_header('Content-Length', '0')
        self.end_headers()


def rest_event(path: str, method: str = 'GET') -> dict:
    """Build the REST API proxy input that the streaming integration actually sends."""
    return {
        'resource': '/{proxy+}',
        'path': path,
        'httpMethod': method,
        'headers': {
            'Host': 'fixture.execute-api.ca-central-1.amazonaws.com',
            'Cookie': 'left=1; right=2',
        },
        'multiValueHeaders': {
            'Host': ['fixture.execute-api.ca-central-1.amazonaws.com'],
            'Cookie': ['left=1; right=2'],
        },
        'queryStringParameters': {'state': 'a+b/='},
        'multiValueQueryStringParameters': {'state': ['a+b/='], 'repeat': ['first', 'second']},
        'requestContext': {
            'stage': 'live',
            'path': '/live' + path,
            'httpMethod': method,
            'identity': {'sourceIp': '127.0.0.1'},
        },
        'body': base64.b64encode('unicode: café'.encode()).decode() if method == 'POST' else None,
        'isBase64Encoded': method == 'POST',
    }


@pytest.mark.skipif(
    not os.environ.get('MCP_LAMBDA_TEST_IMAGE'), reason='Requires an explicitly built Lambda image'
)
def test_real_adapter_rest_stream_envelope_cookies_and_proxy_mapping() -> None:
    """Verify the real binary emits eight-null metadata framing and distinct cookies."""
    server = ThreadingHTTPServer(('0.0.0.0', 0), RuntimeHandler)
    server.daemon_threads = True
    server.events = queue.Queue()
    server.responses = queue.Queue()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    name = 'mcp-adapter-test-' + uuid4().hex[:12]
    fixture_dir = Path(__file__).parent.resolve()
    command = [
        'docker',
        'run',
        '--rm',
        '--name',
        name,
        '--platform',
        'linux/amd64',
        '--add-host',
        'host.docker.internal:host-gateway',
        '-e',
        'MCP_ENABLED=false',
        '-e',
        'AWS_LAMBDA_RUNTIME_API=host.docker.internal:' + str(server.server_port),
        '-e',
        'AWS_LAMBDA_FUNCTION_NAME=markdown-mcp-service',
        '-e',
        'AWS_LAMBDA_FUNCTION_VERSION=1',
        '-e',
        'AWS_LAMBDA_FUNCTION_MEMORY_SIZE=1024',
        '-e',
        'PYTHONPATH=/adapter-tests:/app',
        '-v',
        str(fixture_dir) + ':/adapter-tests:ro',
        '--entrypoint',
        'sh',
        os.environ['MCP_LAMBDA_TEST_IMAGE'],
        '-c',
        '/opt/extensions/lambda-adapter & exec uvicorn adapter_app:app --host 0.0.0.0 --port 8000 --no-access-log --no-proxy-headers',
    ]
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        for event in (
            rest_event('/health'),
            rest_event('/adapter-contract', 'POST'),
            rest_event('/mcp/', 'POST'),
        ):
            server.events.put(event)
            try:
                path, headers, body = server.responses.get(timeout=35)
            except queue.Empty:
                diagnostic = subprocess.check_output(
                    ['docker', 'logs', name], stderr=subprocess.STDOUT, text=True
                )
                raise AssertionError(
                    'Adapter did not return a Runtime API response: ' + diagnostic[-3000:]
                ) from None
            assert path.endswith('/response'), (path, body[:200])
            assert headers['lambda-runtime-function-response-mode'] == 'streaming'
            metadata, payload = body.split(b'\x00' * 8, 1)
            meta = json.loads(metadata)
            if event['path'] == '/health':
                assert meta['statusCode'] == 200
                assert json.loads(payload) == {'status': 'ok'}
            elif event['path'] == '/adapter-contract':
                assert meta['statusCode'] == 200
                cookies = meta.get('cookies', [])
                assert len(cookies) == 2
                assert all('Secure' in cookie and 'HttpOnly' in cookie for cookie in cookies)
                values = json.loads(payload)
                assert values['body'] == 'unicode: café'
                assert values['host'] == 'fixture.execute-api.ca-central-1.amazonaws.com'
                assert values['cookies'] == 'left=1; right=2'
                assert parse_qs(values['query']) == {
                    'state': ['a+b/='],
                    'repeat': ['first', 'second'],
                }
                assert meta['headers']['cache-control'] == 'no-store'
                assert meta['headers']['referrer-policy'] == 'strict-origin'
            else:
                assert meta['statusCode'] == 503
    finally:
        subprocess.run(['docker', 'stop', '--time', '2', name], capture_output=True, timeout=15)
        process.communicate(timeout=15)
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        remaining = subprocess.check_output(
            ['docker', 'ps', '-aq', '--filter', 'name=^/' + name + '$'], text=True
        ).strip()
        assert not remaining, 'Owned adapter container remained after cleanup'
