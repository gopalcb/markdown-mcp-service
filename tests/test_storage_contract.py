"""Protect independently maintained compact storage and conditional overwrite contracts."""

import hashlib
import io
import json
from copy import deepcopy
from pathlib import Path
from unittest.mock import Mock
from uuid import NAMESPACE_URL, uuid5

import pytest
from botocore.exceptions import ClientError

from services.document_service import store_documents


def storage_doubles() -> tuple:
    """Provide stateful conditional S3 objects and DynamoDB returned-attribute acknowledgements."""
    objects, rows = {}, {}
    s3, table = Mock(), Mock()
    table.get_item.side_effect = lambda Key, ConsistentRead: (
        {'Item': deepcopy(rows[Key['file_id']])} if Key['file_id'] in rows else {}
    )

    def publish(
        Key, UpdateExpression, ExpressionAttributeNames, ExpressionAttributeValues, ReturnValues
    ):
        """Apply aliased compact fields and retain independently edited task/creation fields."""
        assert ReturnValues == 'ALL_NEW'
        row = rows.setdefault(Key['file_id'], dict(Key))
        for alias, field in ExpressionAttributeNames.items():
            if alias.startswith('#old'):
                row.pop(field, None)
            elif field in {'task', 'created_at'}:
                row.setdefault(field, ExpressionAttributeValues[':' + alias[1:]])
            else:
                row[field] = ExpressionAttributeValues[':' + alias[1:]]
        return {'Attributes': deepcopy(row)}

    def head(Bucket, Key):
        """Return stored ownership metadata and the exact object concurrency token."""
        item = objects[Key]
        return {'ETag': item['ETag'], 'Metadata': item['Metadata']}

    def put(**options):
        """Enforce S3 preconditions before replacing the current object bytes."""
        key = options['Key']
        prior = objects.get(key)
        if options.get('IfNoneMatch') == '*' and prior is not None:
            raise ClientError({'Error': {'Code': 'PreconditionFailed'}}, 'PutObject')
        if 'IfMatch' in options and (prior is None or options['IfMatch'] != prior['ETag']):
            raise ClientError({'Error': {'Code': 'PreconditionFailed'}}, 'PutObject')
        objects[key] = {
            'Body': options['Body'],
            'Metadata': options['Metadata'],
            'ETag': '"' + hashlib.sha256(options['Body']).hexdigest() + '"',
        }
        return {}

    table.update_item.side_effect = publish
    s3.head_object.side_effect = head
    s3.put_object.side_effect = put
    s3.get_object.side_effect = lambda Bucket, Key: {'Body': io.BytesIO(objects[Key]['Body'])}
    return s3, table, objects, rows


def payload(content: str = '# First', project: str = 'fixture-project') -> dict:
    """Use the fixed MCP owner and server-routed category/basename identity."""
    return {
        'username': 'gopalcbala',
        'file-group': 'learning-chatgpt',
        'files': [{'name': 'example.md', 'content': content, 'metadata': {'project_id': project}}],
    }


def test_storage_snapshot_matches_provenance() -> None:
    """Detect unreviewed changes to the independently adapted shared storage contract."""
    folder = Path(__file__).resolve().parents[1] / 'services'
    provenance = json.loads((folder / 'provenance.json').read_text())
    assert provenance['maintenance'] == 'independently-adapted-compact-storage'
    for name, expected in provenance['sha256'].items():
        assert hashlib.sha256((folder / name).read_bytes()).hexdigest() == expected


def test_same_name_keeps_stable_compact_row_and_latest_bytes() -> None:
    """Unversioned saves retain one ID, creation date and human task while bytes advance."""
    s3, table, objects, rows = storage_doubles()
    first = store_documents(payload(), s3, table, 'bucket', 'request-one')[0]
    expected_id = str(
        uuid5(
            NAMESPACE_URL,
            json.dumps(['gopalcbala', 'learning-chatgpt/example.md'], separators=(',', ':')),
        )
    )
    assert first['status'] == 'READY'
    assert first['file_id'] == expected_id
    created = rows[expected_id]['created_at']
    rows[expected_id]['task'] = {'status': 'In progress', 'revision': 7}
    second = store_documents(payload('# Second'), s3, table, 'bucket', 'request-two')[0]
    assert second == first
    assert len(rows) == len(objects) == 1
    assert rows[expected_id]['created_at'] == created
    assert rows[expected_id]['task'] == {'status': 'In progress', 'revision': 7}
    assert set(rows[expected_id]) == {
        'username',
        'file_id',
        's3_path',
        'file-group',
        'created_at',
        'status',
        'task',
    }
    assert rows[expected_id]['s3_path'] == 's3://bucket/' + first['s3_key']
    stream = s3.get_object(Bucket='bucket', Key=first['s3_key'])['Body']
    with stream:
        assert stream.read() == b'# Second'
    calls = s3.put_object.call_args_list
    assert calls[0].kwargs['IfNoneMatch'] == '*'
    assert 'IfMatch' in calls[1].kwargs
    assert 'VersionId' not in calls[1].kwargs
    table.put_item.assert_not_called()


@pytest.mark.parametrize(
    'metadata,expected',
    [
        ({}, 'READY'),
        ({'upload-token': 'old-native'}, 'INCOMPLETE'),
        ({'source-binding': 'foreign-project'}, 'INCOMPLETE'),
    ],
)
def test_existing_raw_object_adoption_preserves_foreign_source(
    metadata: dict, expected: str
) -> None:
    """Adopt legacy raw objects only; native leases and foreign source bindings block writes."""
    s3, table, objects, rows = storage_doubles()
    key = 'agent-work/raw-markdown/gopalcbala/learning-chatgpt/example.md'
    objects[key] = {'Body': b'legacy', 'Metadata': metadata, 'ETag': '"legacy"'}
    rows['legacy-uuid4'] = {'username': 'gopalcbala', 'file_id': 'legacy-uuid4', 'status': 'READY'}
    result = store_documents(payload(), s3, table, 'bucket', 'request')[0]
    assert result['status'] == expected
    assert rows['legacy-uuid4']['file_id'] == 'legacy-uuid4'
    if expected == 'READY':
        assert objects[key]['Body'] == b'# First'
        assert len(rows) == 2
        assert set(rows[result['file_id']]) == {
            'username',
            'file_id',
            's3_path',
            'file-group',
            'created_at',
            'status',
        }
    else:
        assert objects[key]['Body'] == b'legacy'
        table.update_item.assert_not_called()


def test_other_project_cannot_overwrite_bound_object() -> None:
    """The same routed owner/name cannot silently change its established source project."""
    s3, table, objects, rows = storage_doubles()
    first = store_documents(payload(), s3, table, 'bucket', 'one')[0]
    result = store_documents(payload('# Foreign', 'foreign'), s3, table, 'bucket', 'two')[0]
    assert result['status'] == 'INCOMPLETE'
    assert objects[first['s3_key']]['Body'] == b'# First'
    assert table.update_item.call_count == 1


@pytest.mark.parametrize('failure', ['read', 'object', 'index'])
def test_failed_boundary_never_confirms_success(failure: str) -> None:
    """Read, write and publication errors preserve honest independent per-file outcomes."""
    s3, table, objects, rows = storage_doubles()
    error = ClientError({'Error': {'Code': 'Denied', 'Message': 'private-detail'}}, 'Write')
    if failure == 'read':
        table.get_item.side_effect = error
    elif failure == 'object':
        s3.put_object.side_effect = error
    else:
        table.update_item.side_effect = error
    result = store_documents(payload(), s3, table, 'bucket', 'request')[0]
    assert result['status'] == 'INCOMPLETE'
    assert 'private-detail' not in json.dumps(result)
    assert rows == {}
    if failure != 'index':
        assert objects == {}


def test_missing_index_acknowledgement_cannot_report_ready() -> None:
    """An empty DynamoDB update response never fabricates a successful receipt."""
    s3, table, objects, rows = storage_doubles()
    table.update_item.side_effect = None
    table.update_item.return_value = {}
    result = store_documents(payload(), s3, table, 'bucket', 'request')[0]
    assert result['status'] == 'INCOMPLETE'
