"""Publish acknowledged document bytes using the compact shared index schema."""

FIELDS = {'username', 'file_id', 's3_path', 'file-group', 'created_at', 'status', 'task'}


def publish_file_record(table, record: dict, previous: dict | None = None) -> dict:
    """Update one stable record, preserving its creation date and concurrent task edits."""
    names, values, assignments = {}, {}, []

    for index, (field, value) in enumerate(record.items()):
        if field in {'username', 'file_id'}:
            continue
        alias, parameter = f'#f{index}', f':f{index}'
        names[alias], values[parameter] = field, value
        expression = (
            f'if_not_exists({alias}, {parameter})' if field in {'created_at', 'task'} else parameter
        )
        assignments.append(f'{alias} = {expression}')
    removals = []

    for index, field in enumerate(sorted(set(previous or {}) - FIELDS)):
        alias = f'#old{index}'
        names[alias] = field
        removals.append(alias)
    expression = 'SET ' + ', '.join(assignments)

    if removals:
        expression += ' REMOVE ' + ', '.join(removals)
    response = table.update_item(
        Key={'username': record['username'], 'file_id': record['file_id']},
        UpdateExpression=expression,
        ExpressionAttributeNames=names,
        ExpressionAttributeValues=values,
        ReturnValues='ALL_NEW',
    )
    attributes = response.get('Attributes')

    if not isinstance(attributes, dict) or any(
        attributes.get(field) != record[field]
        for field in ('username', 'file_id', 's3_path', 'file-group', 'status')
    ):
        raise ValueError('Index acknowledgement does not confirm this document')
    return attributes
