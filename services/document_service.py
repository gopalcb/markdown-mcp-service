"""Coordinate independent uploads across S3 and the user-files index."""

from datetime import datetime, timezone
from hashlib import sha256
from uuid import uuid4

from botocore.exceptions import BotoCoreError, ClientError

from services.dynamodb_service import create_file_record, mark_file_ready
from services.logging_service import log_event
from services.s3_service import store_document


def store_documents(payload: dict, s3, table, bucket: str, request_id: str) -> list:
    """Return per-file outcomes; leave interrupted records PENDING for reconciliation."""
    results = []

    for document in payload['files']:
        file_id = str(uuid4())
        username = payload['username']
        group = document.get(
            'file-group', payload.get('file-group', document.get('category', 'workflow-docs'))
        )
        stored_name = document['name']
        record = {
            'username': username,
            'file_id': file_id,
            'filename': document['name'],
            'category': group,
            'file-group': group,
            'metadata': document['metadata'],
            'created_at': datetime.now(timezone.utc).isoformat(),
            's3_bucket': bucket,
            's3_key': f'agent-work/raw-markdown/{username}/{group}/{stored_name}',
            'storage_layout': 'raw-markdown-v2',
            'size_bytes': len(document['content'].encode('utf-8')),
            'sha256': sha256(document['content'].encode('utf-8')).hexdigest(),
            'status': 'PENDING',
            'request_id': request_id,
            **payload.get('device', {}),
        }
        result = {'name': document['name'], 'file_id': file_id}

        try:
            create_file_record(table, record)

            record['s3_version_id'] = store_document(s3, bucket, record, document['content'])

            mark_file_ready(table, record)
        except (BotoCoreError, ClientError, ValueError) as error:
            log_event('upload_incomplete', request_id, file_id, type(error).__name__)

            result.update(
                status='INCOMPLETE',
                error='Storage not confirmed; inspect this file_id before retrying',
            )
        else:
            log_event('upload_ready', request_id, file_id)

            result.update(status='READY', s3_key=record['s3_key'])

        results.append(result)

    return results
