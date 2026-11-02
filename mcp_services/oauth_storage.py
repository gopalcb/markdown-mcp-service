"""Persist encrypted OAuth state with strong reads and atomic redemption claims."""

import asyncio
import hashlib
import math
import time

from botocore.exceptions import ClientError
from key_value.aio._utils.managed_entry import ManagedEntry
from key_value.aio._utils.serialization import BasicSerializationAdapter
from key_value.aio.stores.base import BaseStore


class OAuthStateStore(BaseStore):
    """Adapt the provisioned DynamoDB table to the OAuth library's store contract."""

    def __init__(self, client, table_name: str, namespace: str = ''):
        """Use a provisioned table and optional isolated preview collection namespace."""
        super().__init__()
        self.client = client
        self.table_name = table_name
        self.namespace = namespace
        self.serializer = BasicSerializationAdapter()

    def item_key(self, collection: str, key: str) -> dict:
        """Hash keys so authorization codes and client identifiers are not stored raw."""
        return {
            'collection': {
                'S': self.namespace + ':' + collection if self.namespace else collection
            },
            'key': {'S': hashlib.sha256(key.encode()).hexdigest()},
        }

    async def _get_managed_entry(self, collection: str, key: str) -> ManagedEntry | None:
        """Implement the library hook with a strongly consistent, expiry-checked read."""
        response = await asyncio.to_thread(
            self.client.get_item,
            TableName=self.table_name,
            Key=self.item_key(collection, key),
            ConsistentRead=True,
        )
        item = response.get('Item')
        if not item or int(item.get('ttl', {'N': '0'})['N']) <= time.time():
            return None
        return self.serializer.load_json(item['value']['S'])

    async def _put_managed_entry(
        self, collection: str, key: str, managed_entry: ManagedEntry
    ) -> None:
        """Implement the library hook; expire otherwise permanent client records after 90 days."""
        expires = (
            managed_entry.expires_at.timestamp()
            if managed_entry.expires_at
            else time.time() + 90 * 86400
        )
        item = self.item_key(collection, key) | {
            'value': {'S': self.serializer.dump_json(managed_entry)},
            'ttl': {'N': str(math.ceil(expires))},
        }
        await asyncio.to_thread(self.client.put_item, TableName=self.table_name, Item=item)

    async def _delete_managed_entry(self, key: str, collection: str) -> bool:
        """Implement the library hook and return whether a record actually existed."""
        response = await asyncio.to_thread(
            self.client.delete_item,
            TableName=self.table_name,
            Key=self.item_key(collection, key),
            ReturnValues='ALL_OLD',
        )
        return bool(response.get('Attributes'))

    async def claim_once(self, kind: str, value: str, expires_at: float) -> bool:
        """Atomically claim a validated grant across overlapping tasks; never retry redemption."""
        item = self.item_key('redemption-' + kind, value) | {
            'ttl': {'N': str(math.ceil(max(expires_at, time.time() + 600)))},
        }
        try:
            await asyncio.to_thread(
                self.client.put_item,
                TableName=self.table_name,
                Item=item,
                ConditionExpression='attribute_not_exists(#key)',
                ExpressionAttributeNames={'#key': 'key'},
            )
        except ClientError as error:
            if error.response['Error']['Code'] == 'ConditionalCheckFailedException':
                return False
            raise
        return True
