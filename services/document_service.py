"""Coordinate latest-only Markdown uploads with the shared compact document index."""

import json
from datetime import datetime, timezone
from hashlib import sha256
from urllib.parse import quote
from uuid import NAMESPACE_URL, uuid5

from botocore.exceptions import BotoCoreError, ClientError

from services.dynamodb_service import publish_file_record
from services.logging_service import log_event
from services.s3_service import store_document


def store_documents(payload: dict, s3, table, bucket: str, request_id: str) -> list:
    """Return independent READY receipts only after both object and index acknowledgements."""
    results = []
    username = payload['username']

    for document in payload['files']:
        group = document.get(
            'file-group', payload.get('file-group', document.get('category', 'workflow-docs'))
        )
        relative = group + '/' + document['name']
        source = 'docs/' + relative
        file_id = str(uuid5(NAMESPACE_URL, json.dumps([username, relative], separators=(',', ':'))))
        key = {'username': username, 'file_id': file_id}
        s3_key = f'agent-work/raw-markdown/{username}/{relative}'
        path = f's3://{bucket}/{quote(s3_key, safe="/")}'
        project_id = document.get('metadata', {}).get('project_id', '')
        binding = sha256((str(project_id) + '\n' + source).encode()).hexdigest()
        result = {'name': document['name'], 'file_id': file_id}

        try:
            existing = table.get_item(Key=key, ConsistentRead=True).get('Item')
            legacy = False

            if existing:
                if existing.get('upload_token') or existing.get('status') != 'READY':
                    raise ValueError('An earlier upload is unresolved')

                if 's3_path' in existing:
                    if existing['s3_path'] != path:
                        raise ValueError('Stored document location differs from the canonical path')
                else:
                    legacy = (
                        existing.get('s3_bucket') == bucket
                        and existing.get('s3_key') == s3_key
                        and existing.get('source_path') == source
                        and existing.get('metadata', {}).get('project_id', '') == project_id
                    )

                    if not legacy:
                        raise ValueError('Another source occupies this document identity')

            store_document(s3, bucket, s3_key, document['content'], binding, bool(existing))
            record = {
                **key,
                's3_path': path,
                'file-group': group,
                'created_at': existing['created_at']
                if existing
                else datetime.now(timezone.utc).isoformat(),
                'status': 'READY',
            }

            publish_file_record(table, record, existing)
        except (BotoCoreError, ClientError, ValueError) as error:
            log_event('upload_incomplete', request_id, file_id, type(error).__name__)
            result.update(
                status='INCOMPLETE',
                error='Storage not confirmed; inspect this file_id before retrying',
            )
        else:
            log_event('upload_ready', request_id, file_id)
            result.update(status='READY', s3_key=s3_key)

        results.append(result)

    return results
