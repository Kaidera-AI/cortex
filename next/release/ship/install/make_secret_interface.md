# S1 make_secret: interface and the one open join (day 3, RED only)
Source: this directory, `make_secret.py` (signature and contract in its docstring), tests `tests/test_make_secret.py`. No implementation, no secret, no key, no custody effect.

**Frozen, reused:** five purposes and import order `bootstrap, migrator, api_request, probe, installer_enrollment` (`roles.A_PURPOSES`); Core `open_installer_custody(config, *, unlock_fd, key_id, deadline)`
and `owner.import_credential(purpose, bytes)` (custody contract 35977ae5); `roles.MIN_SECRET`; one config, one deadline, one owner. **Not chosen:** the J06 producer of the five credentials and of
the unlock key; the interface takes it as an explicit argument with no default.

**Output:** names only (`imported`, `bootstrap_delivered`); values never leave the call. **Bootstrap input:** the SAME bytes imported as `bootstrap` go once to `bootstrap_sink` after all five imports
(S4 prelude, stock initdb password transfer); never argv, environment, log, plaintext file or a second generation.

## The join that is NOT specified (kept RED / NOT RUN)
`open_installer_custody` needs the Core `config`, which `write_config` (S2) writes; the accepted order lists `make_secret` BEFORE `write_config`. Two bounded options:
 A. **Config first.** Order becomes `write_config` then `make_secret` (the config holds references and identity only, no secret). `install.sh` UNWIRED order and the S2 note change; one process
    opens custody on the written config, imports, and hands the bootstrap bytes to the prelude in the same bounded invocation. Smallest change; it reorders two phases, so it needs Cox's ruling.
 B. **Secrets first, import later.** Keep the order; `make_secret` only produces and holds the bytes and a long-lived installer process imports them after `write_config`, still inside one
    invocation. Keeps the order but needs a single Python orchestrator process spanning three shell phases (new mechanism) and holds secrets across a phase boundary.
Recommendation: A. Until ruled, neither is implemented and the behaviour stays RED/NOT RUN.
