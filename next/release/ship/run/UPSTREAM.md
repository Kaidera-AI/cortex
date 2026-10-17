# ship/run upstream

- Kubernetes Pod format as read by `podman kube play` (podman-kube-play.1, podman 6.0.2 on the
  team Mac, 7.0.3 in Rocky 10 AppStream; Apache-2.0). Format only; no code copied. Output is JSON
  text, which is valid YAML 1.2, so no YAML library or template engine is added.
- Database argv: `core-init` and `core-server` vectors of
  `next/contracts/runtime/runtime-operations-v1.json` (ours, MIT), checked against Core's
  `RC0_SHA256` pin (51de95ac63cd19e59b046a9db913e38aa790687da48a87b2174468c6873a873b).
- Core (ours, MIT): `runtime.credentials.read_private_file`, `runtime.bootstrap_config.decode_config`
  and `MODE_PURPOSES`, `runtime.contracts.decode`, `release.codec.sha256`, `runtime.signed_member.Refusal`.
  render adds no parser, pin or refusal type of its own.
- Python standard library otherwise (PSF licence).
