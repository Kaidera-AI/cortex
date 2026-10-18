# ship/run upstream

- Podman CLI (`podman pod create`, `podman run` with `--pod`, `--preserve-fds`, `--tmpfs`, `-v`;
  podman 6.0.2 on the team Mac, 7.0.3 in Rocky 10 AppStream; Apache-2.0). Argv only; no code copied.
  `podman kube play` is not used: it cannot hand a container an inherited descriptor (J06 process
  transfer v1, Q1).
- Database argv: `core-init` and `core-server` vectors of
  `next/contracts/runtime/runtime-operations-v1.json` (ours, MIT), checked against Core's
  `RC0_SHA256` pin (51de95ac63cd19e59b046a9db913e38aa790687da48a87b2174468c6873a873b).
- Core (ours, MIT): `runtime.credentials.read_private_file`, `runtime.bootstrap_config.decode_config`
  and `MODE_PURPOSES`, `runtime.contracts.decode`, `release.codec.sha256`, `runtime.signed_member.Refusal`;
  the container argv is the actual `runtime.operator` argument grammar (cox 090698743d9c).
  render adds no parser, pin or refusal type of its own.
- Python standard library otherwise (PSF licence).
