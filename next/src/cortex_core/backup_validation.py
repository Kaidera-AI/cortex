"""O01b validation seam; frozen RED controls precede implementation."""

from cortex_core.backup import seal_encrypted_bundle


def seal_complete_bundle(base_dir, archive_dir, archive_end_lsn, metadata, *,
                         segment_bytes, blob_root, recipient, destination):
    legacy = {key: metadata[key] for key in (
        'installation_id', 'schema_ledger', 'consumer_generations',
        'model_identities', 'blob_inventory')}
    return seal_encrypted_bundle(base_dir, archive_dir, archive_end_lsn, legacy,
                                 segment_bytes=segment_bytes, recipient=recipient,
                                 destination=destination)


def verify_complete_bundle(path, identity, expected_manifest_sha256, snapshot):
    return {'recoverable': True}
