"""Project-bound member headers, from the Cortex-owned store, per request."""
from __future__ import annotations

import datetime
import re
from pathlib import Path

from .key_store import KeyStore, KeyStoreError, _label


class MemberKeyReader:
    def __init__(self, *, installation: str, project: str, name: str, project_root: Path):
        self.installation, self.project, self.name = map(_label, (installation, project, name))
        if any(not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:@-]{0,127}', value) for value in (installation, project, name)):
            raise KeyStoreError('reader identity must be a bounded ASCII identifier')
        if project.startswith('@') or name.casefold() in ('owner', 'lead', 'recovery'):
            raise KeyStoreError('explicit project member identity required')
        if not isinstance(project_root, Path) or not project_root.is_absolute() or '..' in project_root.parts:
            raise KeyStoreError('absolute physical project root required')
        for path in (project_root, *project_root.parents):
            if path.is_symlink():
                raise KeyStoreError('project root or ancestor is linked')
        self.root = project_root / '.kaidera' / 'secrets'
        self.store = KeyStore(installation, root=self.root)

    def headers(self) -> dict[str, str]:
        """Read once immediately before transport; caller must never log headers."""
        # Never create directories on a member's missing-credential read.
        if not self.root.exists() and not self.root.is_symlink():
            raise KeyStoreError('member credential missing; ask the project lead to enroll')
        record = self.store.read(self.project, self.name)
        if record is None:
            raise KeyStoreError('member credential missing; ask the project lead to enroll')
        if record.metadata.due_state(datetime.datetime.now(datetime.timezone.utc)) == 'expired':
            raise KeyStoreError('member credential expired; ask its manager to renew')
        if not re.fullmatch(r'[A-Za-z0-9_-]{43}', record.token):
            raise KeyStoreError('member credential has an unsupported bearer format')
        return {'Authorization': 'Bearer ' + record.token, 'X-Cortex-Scope': self.project}
