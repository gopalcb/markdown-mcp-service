"""Emit structured operational events without document bodies or metadata."""

import json
import logging

LOGGER = logging.getLogger('document-api')

LOGGER.setLevel(logging.INFO)


def log_event(event: str, request_id: str, file_id: str = '', error_type: str = '') -> None:
    """Write only identifiers and safe error types to Lambda's CloudWatch stream."""

    LOGGER.info(
        json.dumps(
            {'event': event, 'request_id': request_id, 'file_id': file_id, 'error_type': error_type}
        )
    )
