# ship/run upstream

- Kubernetes Pod format as read by `podman kube play` (podman-kube-play.1, podman 6.0.2 on the
  team Mac, 7.0.3 in Rocky 10 AppStream; Apache-2.0). Format only; no code copied. Output is JSON
  text, which is valid YAML 1.2, so no YAML library or template engine is added.
- Database argv: `core-init` and `core-server` vectors of
  `next/contracts/runtime/runtime-operations-v1.json` (ours, MIT), sha256
  51de95ac63cd19e59b046a9db913e38aa790687da48a87b2174468c6873a873b, pinned in `render.py`.
- Python standard library only (PSF licence).
