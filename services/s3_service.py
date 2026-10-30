"""Write one versioned private Markdown object in its user and category partition."""


def store_document(client, bucket: str, record: dict, content: str) -> str:
    """Persist the object; propagate errors so the index is never marked ready early."""

    response = client.put_object(
        Bucket=bucket,
        Key=record['s3_key'],
        Body=content.encode('utf-8'),
        ContentType='text/markdown; charset=utf-8',
        ServerSideEncryption='AES256',
        **(
            {
                'Metadata': {
                    key.replace('_', '-'): record[key]
                    for key in ('device_id', 'device_mac', 'device_type')
                    if record.get(key)
                }
            }
            if 'device_id' in record
            else {}
        ),
    )
    version = response.get('VersionId')

    if not isinstance(version, str) or not version or version == 'null':
        raise ValueError('A durable S3 version is required before marking READY')

    return version
