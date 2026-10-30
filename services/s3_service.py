"""Write latest private Markdown bytes with source-bound conditional S3 writes."""

from botocore.exceptions import ClientError


def store_document(
    client,
    bucket: str,
    key: str,
    content: str,
    binding: str,
    existing: bool = False,
) -> None:
    """Acknowledge one conditional write; reject collisions and never require a version ID."""
    options = {
        'Bucket': bucket,
        'Key': key,
        'Body': content.encode('utf-8'),
        'ContentType': 'text/markdown; charset=utf-8',
        'ServerSideEncryption': 'AES256',
        'Metadata': {'source-binding': binding},
    }

    if existing:
        head = client.head_object(Bucket=bucket, Key=key)
        stored_binding = head.get('Metadata', {}).get('source-binding')

        if stored_binding != binding and not (
            stored_binding is None and not head.get('Metadata', {}).get('upload-token')
        ):
            raise ValueError('Existing object belongs to another source')
        options['IfMatch'] = head['ETag']
    else:
        options['IfNoneMatch'] = '*'

    try:
        client.put_object(**options)
    except ClientError as error:
        if existing or error.response['Error']['Code'] not in {'PreconditionFailed', '412'}:
            raise
        # A definite collision wrote no bytes; verify source ownership or legacy raw adoption.
        head = client.head_object(Bucket=bucket, Key=key)

        stored_binding = head.get('Metadata', {}).get('source-binding')

        if stored_binding != binding and not (
            stored_binding is None and not head.get('Metadata', {}).get('upload-token')
        ):
            raise ValueError('Existing object belongs to another source') from None
        options.pop('IfNoneMatch')
        client.put_object(**options, IfMatch=head['ETag'])
