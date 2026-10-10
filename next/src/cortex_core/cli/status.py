"""Executable deny-all baseline for the accepted status unit's frozen REDs."""
import asyncio
import json
import sys

BUDGET_SECONDS = 2.0
MAX_BODY_BYTES = 16 * 1024


async def run(argv, *, transport=None, out=None):
    out = sys.stdout if out is None else out
    packet = {"schema": "cortex.status.v1", "health": None, "client_error": "endpoint_refused"}
    if "--json" in argv:
        print(json.dumps(packet), file=out)
    else:
        print("Cortex status: unavailable\nCore: unknown\nConductor: unknown\nClient error: endpoint_refused", file=out)
    return 64


def main(argv=None, *, transport=None, out=None):
    return asyncio.run(run(sys.argv[1:] if argv is None else argv, transport=transport, out=out))
