"""Adapt validated MCP input to the exact production document storage service."""

import asyncio
from uuid import uuid4

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from mcp_services.validation_service import UploadBatch, prepare_payload
from services.document_service import store_documents


def write_documents(payload: dict, config: dict) -> list:
    """Use task-role credentials and no automatic write retries; always close clients."""
    aws_config = Config(connect_timeout=5, read_timeout=15, retries={'total_max_attempts': 1})
    s3 = boto3.client('s3', config=aws_config)
    dynamodb = None

    try:
        dynamodb = boto3.resource('dynamodb', config=aws_config)

        return store_documents(
            payload,
            s3,
            dynamodb.Table(config['USER_FILES_TABLE']),
            config['S3_BUCKET'],
            str(uuid4()),
        )
    finally:
        s3.close()

        if dynamodb is not None:
            dynamodb.meta.client.close()


async def save_batch(payload: UploadBatch, config: dict) -> dict:
    """Report saved versus uncertain outcomes only after the shared READY transition."""
    prepared = prepare_payload(payload)

    try:
        results = await asyncio.to_thread(write_documents, prepared, config)
    except (BotoCoreError, ClientError):
        raise ValueError('Storage not confirmed; reconcile before retrying') from None

    return {'success': all(item['status'] == 'READY' for item in results), 'files': results}
