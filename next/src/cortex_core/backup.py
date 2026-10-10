"""O01a backup producer. Implementation follows frozen RED controls."""


class BackupError(ValueError):
    """A backup set is incomplete or cannot be sealed."""


def build_manifest(base_dir, archive_dir, archive_end_lsn, metadata, *, segment_bytes):
    return {}
