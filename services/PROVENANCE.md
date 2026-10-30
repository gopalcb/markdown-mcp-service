# Work Organizer storage contract

The initial five-module snapshot originated from gb-systems-and-agents commit
06967a81915a21dc48fa102f8771dbd9a73b195a at
work-organizer-portal/aws-infrastructure/document-api/src/services.
The current document, S3 and DynamoDB services are independently maintained
adaptations, not exact copies of that historical source. `provenance.json` pins
the current local module bytes and retains the original upstream identity.

The compact contract was compared with the Softrobots latest-storage worktree
path_document_service.py and document_storage_service.py on 2026-10-30. Stable
UUID5 uses owner plus canonical group/name; source binding uses project_id plus
`docs/<group>/<name>`. Writes require conditional S3 and DynamoDB acknowledgements,
without S3 VersionId or PENDING rows. The persisted fields are username, file_id,
s3_path, file-group, created_at, status, and optional existing task. Creation date
and concurrent task edits are preserved. MCP receipts retain name/file_id/status/s3_key.

Legacy raw objects may be adopted only without a foreign binding or unresolved
native upload-token. Existing historical UUID4 index rows remain unchanged; no
query, scan, bulk migration or deletion occurs. Interrupted outcomes remain
INCOMPLETE and require reconciliation; SDK automatic write retries remain disabled.

This repository builds independently. Contract tests exercise source binding,
legacy adoption, conditional latest writes, compact publication and failed batches.
A hash match verifies the local reviewed source bytes, not equality with upstream
or proof of live AWS behavior.
