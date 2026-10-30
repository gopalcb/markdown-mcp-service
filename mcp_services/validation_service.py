"""Validate deployment settings and whole Markdown batches before storage."""

import os
import re
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Metadata(BaseModel):
    """Bounded editorial metadata; relative paths do not alter storage keys."""

    model_config = ConfigDict(extra='forbid', hide_input_in_errors=True)
    project: str = Field(default='chatgpt-web-chat', max_length=100)
    relative_path: str = Field(default='', max_length=200)

    @field_validator('relative_path')
    @classmethod
    def validate_path(cls, value: str) -> str:
        """Accept only safe slash-separated segments."""

        if value and any(not re.fullmatch(r'[A-Za-z0-9_-]+', part) for part in value.split('/')):
            raise ValueError('Invalid relative path')

        return value


class MarkdownFile(BaseModel):
    """One safe basename and UTF-8 Markdown document."""

    model_config = ConfigDict(extra='forbid', hide_input_in_errors=True)
    name: str = Field(pattern=r'^[A-Za-z0-9][A-Za-z0-9._-]{0,119}\.md$')
    content: str = Field(max_length=262144)
    metadata: Metadata = Field(default_factory=Metadata)


class UploadBatch(BaseModel):
    """Personal upload input; authorization independently binds the subject."""

    model_config = ConfigDict(extra='forbid', populate_by_name=True, hide_input_in_errors=True)
    username: str = Field(default='gopalcbala', pattern=r'^gopalcbala$')
    file_group: str = Field(
        default='learning-chatgpt', alias='file-group', pattern=r'^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$'
    )
    files: list[MarkdownFile] = Field(min_length=1, max_length=10)


def prepare_payload(batch: UploadBatch) -> dict:
    """Validate every byte and destination before producing the shared storage contract."""
    names = set()
    size = 0

    for document in batch.files:
        if document.name in names or '..' in document.name:
            raise ValueError('Duplicate or unsafe destination')

        names.add(document.name)
        body = document.content.encode('utf-8')

        if len(body) > 256 * 1024:
            raise ValueError('File exceeds 256 KiB')
        size += len(body)

    if size > 2_000_000:
        raise ValueError('Batch exceeds 2 MB')

    return batch.model_dump(by_alias=True)


def load_config() -> dict:
    """Fail startup for incomplete enabled configurations; permit deny-all bootstrap."""
    enabled = os.environ.get('MCP_ENABLED', 'true').lower() == 'true'
    config = {'enabled': enabled}

    if not enabled:
        return config

    for name in (
        'PUBLIC_BASE_URL',
        'COGNITO_ISSUER',
        'COGNITO_CLIENT_ID',
        'MCP_RESOURCE',
        'MCP_WRITE_SCOPE',
        'ALLOWED_SUB',
        'S3_BUCKET',
        'USER_FILES_TABLE',
    ):
        value = os.environ.get(name, '')

        if not value or value.strip() != value or any(ord(char) < 32 for char in value):
            raise ValueError(f'Missing or invalid {name}')
        config[name] = value
    base = config['PUBLIC_BASE_URL']
    parsed = urlsplit(base)

    if (
        parsed.scheme != 'https'
        or not parsed.hostname
        or parsed.path
        or parsed.query
        or parsed.fragment
        or parsed.username
        or parsed.password
    ):
        raise ValueError('PUBLIC_BASE_URL must be an HTTPS origin without a trailing slash')

    if config['MCP_RESOURCE'] != base + '/mcp/':
        raise ValueError('MCP_RESOURCE must exactly match PUBLIC_BASE_URL/mcp/')

    if not re.fullmatch(
        r'https://cognito-idp\.[a-z0-9-]+\.amazonaws\.com/[A-Za-z0-9_-]+', config['COGNITO_ISSUER']
    ):
        raise ValueError('Invalid Cognito issuer')

    origin_host = os.environ.get('MCP_ORIGIN_HOST', '')
    if origin_host:
        if not re.fullmatch(r'[a-z0-9-]+\.ecs\.ca-central-1\.on\.aws', origin_host):
            raise ValueError('MCP_ORIGIN_HOST must be the explicit regional ECS hostname')
        config['MCP_ORIGIN_HOST'] = origin_host
    return config
