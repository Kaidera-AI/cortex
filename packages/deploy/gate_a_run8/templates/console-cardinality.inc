keys = re.findall(r'^ssh-ed25519\s+([A-Za-z0-9+/=]+)(?:\s|$)', text, flags=re.M)
if len(set(keys)) == 0:
    raise SystemExit('fresh AWS console ED25519 host key not available; no trust-on-first-use')
if len(set(keys)) > 1:
    raise SystemExit('multiple distinct AWS console ED25519 host keys; permanent identity refusal')
expected = keys[0]
