# UPSTREAM (recipes/api: inputs changed on 17 Oct, J01)
| input | source | pinned | licence | obligation / change |
|---|---|---|---|---|
| asyncpg 0.32.0 (cp314 musllinux_1_2, aarch64 + x86_64) | PyPI https://pypi.org/project/asyncpg/0.32.0/ | sha256 `2c6366841a792d0a4d16991de240a8053b7c4772a18a5f27fa6fad09c0e359fb` (arm64), `c3ef1dfd11919280e011ffd1c873323c5088a94fd2c3f77946a5250cf306e2eb` (amd64); equal to the PyPI-published digests | Apache-2.0 | LICENSE kept in the wheel's dist-info; no NOTICE file in the wheel; unmodified. Added because the runtime calls `_require_asyncpg()` (runtime/pg_adapters.py, auth.py:144). No dependencies on Python 3.14 |
| recipe, apk pins, other 18 wheel locks | carried unchanged from branch `nix/ga2-recipes-20261016` (content of 514299a9), review input, not an acceptance of ga2 | as in the files | per ship-build-inputs.md licence table (psycopg LGPL-3.0, certifi MPL-2.0: CHECK items for the CTO) | none changed |
The ONE archive for this line is `/Volumes/WD-B-4TB/DevVault/helix/output/Cortex/nix-a-day1-assets-20261017` (116 files; the frozen `nix-ga3-assets-20261016` is untouched).
