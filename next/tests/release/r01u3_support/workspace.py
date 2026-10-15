"""TEST ONLY: private inert fixture storage belonging to this source checkout."""
from pathlib import Path
import tempfile


def workspace_output():
    workspace=Path(__file__).resolve().parents[4]
    # Helix keeps task checkouts under its one project root. CI is a checkout.
    if workspace.parent.name=='.worktrees':workspace=workspace.parent.parent
    root=workspace/'output/r01u3-fixtures'
    root.mkdir(mode=0o700,parents=True,exist_ok=True)
    return root.resolve(strict=True)


def temporary_directory(prefix):
    return tempfile.TemporaryDirectory(prefix=prefix,dir=workspace_output())


def renamed_directory_observation(old, renamed, before):
    """Observe actual filesystem behavior without raising on an absent old name."""
    import os
    names=sorted(entry.name for entry in os.scandir(renamed.parent))
    after=renamed.stat()
    try:alias=old.stat()
    except FileNotFoundError:alias=None
    identity=(before.st_dev,before.st_ino)
    assert (after.st_dev,after.st_ino)==identity
    assert old.name not in names and renamed.name in names
    if alias is not None:assert (alias.st_dev,alias.st_ino)==identity
    observed=dict(same_inode=True,old_resolves=alias is not None,exact_present=False,
                names=names,dev=after.st_dev,inode=after.st_ino,
                reason='member_path_alias' if alias is not None else 'member_custody_changed')
    print('U3_CASE_OBSERVATION '+__import__('json').dumps(dict(old=str(old),renamed=str(renamed),**observed),sort_keys=True))
    return observed
