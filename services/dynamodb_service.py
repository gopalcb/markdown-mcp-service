"""Maintain one upload record keyed by username and file ID."""


def create_file_record(table, record: dict) -> None:
    """Record intent before writing objects so interrupted uploads can be inspected."""

    table.put_item(Item=record, ConditionExpression='attribute_not_exists(file_id)')


def mark_file_ready(table, record: dict) -> None:
    """Publish readiness only after the S3 object has been acknowledged."""

    table.update_item(
        Key={'username': record['username'], 'file_id': record['file_id']},
        UpdateExpression='SET #status = :ready, s3_version_id = :version',
        ConditionExpression='#status = :pending',
        ExpressionAttributeNames={'#status': 'status'},
        ExpressionAttributeValues={
            ':ready': 'READY',
            ':pending': 'PENDING',
            ':version': record['s3_version_id'],
        },
    )
