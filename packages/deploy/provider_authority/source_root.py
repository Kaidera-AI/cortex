"""Locate the source checkout, or report honestly that there isn't one.

Four modules resolved the repo root as ``Path(__file__).resolve().parents[3]``, which
encodes the NATIVE directory depth: ``<repo>/local-cortex/console/app/<module>.py``.

The container image copies only the app package, so a module lives at
``/app/app/<module>.py`` — three parents exist (`/app/app`, `/app`, `/`) and indexing the
fourth raises. `managed_runtime.py` did it at MODULE level, so importing it inside the
container raised ``IndexError: 3`` and turned every ``GET /settings/{project}/runtime``
into a 500 on marlow. The other three appended the path to a candidate list and would
have tolerated it being absent — but the indexing raised before that tolerance ran.

There is no repo in the image at all: `/app` holds only `app/`, `requirements.txt` and
`spa/`. So the honest answer for a containerised console is "no source checkout", not a
guessed path — the features that want it (host container control, the graph-blast script,
the cortex-log CLI) are host-only by nature.

Searching upward for a marker rather than counting parents also means a future re-nesting
of the console cannot silently break this again.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

# Files/dirs that only exist at the root of a real Kaidera OS checkout. `.agents` is the
# harness tree every caller here actually wants; `install.sh` disambiguates a bare
# `.agents` copied somewhere else.
_MARKERS = ("install.sh", ".agents")

# Bound the walk so a pathological mount cannot turn this into a filesystem crawl.
_MAX_DEPTH = 6


@lru_cache(maxsize=1)
def source_root() -> Path | None:
    """The Kaidera OS source checkout containing this module, or None.

    None means "running from the container image, or otherwise outside a checkout".
    Callers must treat that as "this host-only feature is unavailable" and degrade —
    never substitute a guessed path.
    """
    here = Path(__file__).resolve()
    for parent in list(here.parents)[:_MAX_DEPTH]:
        if all((parent / marker).exists() for marker in _MARKERS):
            return parent
    return None
