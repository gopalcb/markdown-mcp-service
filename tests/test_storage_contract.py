"""Protect the independent source snapshot and version-pinned overwrite contract."""

import hashlib
import json
from pathlib import Path
from unittest.mock import Mock

from services.document_service import store_documents


def test_storage_snapshot_matches_provenance() -> None:
    """Fail when vendored shared logic changes without an explicit provenance review."""
    folder = Path(__file__).resolve().parents[1] / 'services'
    provenance = json.loads((folder / 'provenance.json').read_text())
    for name, expected in provenance['sha256'].items():
        assert hashlib.sha256((folder / name).read_bytes()).hexdigest() == expected


def test_same_name_keeps_key_and_pins_distinct_versions() -> None:
    """Repeated saves retain separate file receipts while intentionally versioning one key."""
    payload = {
        'username': 'gopalcbala',
        'file-group': 'learning-chatgpt',
        'files': [{'name': 'example.md', 'content': '# First', 'metadata': {}}],
    }
    s3, table = Mock(), Mock()
    s3.put_object.side_effect = [{'VersionId': 'first'}, {'VersionId': 'second'}]
    first = store_documents(payload, s3, table, 'bucket', 'request-one')[0]
    payload['files'][0]['content'] = '# Second'
    second = store_documents(payload, s3, table, 'bucket', 'request-two')[0]
    assert first['status'] == second['status'] == 'READY'
    assert first['file_id'] != second['file_id']
    assert first['s3_key'] == second['s3_key']
    assert [
        call.kwargs['ExpressionAttributeValues'][':version']
        for call in table.update_item.call_args_list
    ] == ['first', 'second']
