#!/bin/sh
# Finite PKI setup. Only this initializer receives the dedicated CA volume.
set -eu
umask 077

action=${1:-init}
case "$action" in init|rotate-leaves) ;; *) echo 'usage: init.sh [init|rotate-leaves]' >&2; exit 2 ;; esac
tls_root=${CORTEX_TLS_ROOT:-/tls}
case "$tls_root" in /*) ;; *) echo 'TLS root must be absolute' >&2; exit 2 ;; esac
[ "$tls_root" != / ] || { echo 'TLS root cannot be /' >&2; exit 2; }
server_uid=${CORTEX_TLS_SERVER_UID:-999}
client_uid=${CORTEX_TLS_CLIENT_UID:-10001}
server_gid=${CORTEX_TLS_SERVER_GID:-999}
client_gid=${CORTEX_TLS_CLIENT_GID:-10001}
for owner in "$server_uid" "$server_gid" "$client_uid" "$client_gid"; do
    case "$owner" in ''|*[!0-9]*) echo 'TLS ownership must be numeric' >&2; exit 2 ;; esac
done

fail() { echo "cortex TLS: $*" >&2; exit 1; }
for dir in "$tls_root" "$tls_root/ca" "$tls_root/server" "$tls_root/app" "$tls_root/admin"; do
    [ ! -L "$dir" ] || fail 'TLS directories must not be symlinks'
    mkdir -p "$dir"
done
ca_dir=$tls_root/ca
mkdir "$ca_dir/.lock" 2>/dev/null || fail 'initializer already running (or stale lock needs inspection)'
work=''
cleanup() {
    if [ -n "$work" ]; then
        for name in ca.crt ca.key ca.cnf server.crt server.key server.csr server.cnf app.crt app.key app.csr app.cnf admin.crt admin.key admin.csr admin.cnf; do
            rm -f "$work/$name"
        done
        rmdir "$work"
    fi
    rmdir "$ca_dir/.lock"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

mode_of() { stat -c '%a' "$1" 2>/dev/null || stat -f '%Lp' "$1"; }
uid_of() { stat -c '%u' "$1" 2>/dev/null || stat -f '%u' "$1"; }
gid_of() { stat -c '%g' "$1" 2>/dev/null || stat -f '%g' "$1"; }
regular() { [ -f "$1" ] && [ ! -L "$1" ] || fail 'missing, partial, or symlinked PKI; refusing regeneration'; }
matching_key() {
    cert_mod=$(openssl x509 -noout -modulus -in "$1")
    key_mod=$(openssl rsa -noout -modulus -in "$2" 2>/dev/null)
    [ "$cert_mod" = "$key_mod" ] || fail 'certificate does not match private key'
}
validate_ca() {
    regular "$ca_dir/ca.crt"
    regular "$ca_dir/ca.key"
    [ "$(mode_of "$ca_dir/ca.key")" = 400 ] || fail 'CA key must have mode 0400'
    [ "$(uid_of "$ca_dir/ca.key")" = "$(id -u)" ] || fail 'CA key must belong to initializer owner'
    matching_key "$ca_dir/ca.crt" "$ca_dir/ca.key"
    openssl verify -CAfile "$ca_dir/ca.crt" "$ca_dir/ca.crt" >/dev/null
    openssl x509 -in "$ca_dir/ca.crt" -noout -text | grep -q 'CA:TRUE' || fail 'CA certificate is not a CA'
    # Leave enough CA lifetime to issue a full 365-day leaf on explicit rotation.
    openssl x509 -in "$ca_dir/ca.crt" -noout -checkend 31536000 >/dev/null || fail 'CA needs operator renewal before leaf issuance'
}
validate_leaf() {
    role=$1 cn=$2 purpose=$3 owner_uid=$4 owner_gid=$5
    leaf_dir=$tls_root/$role
    for filename in ca.crt tls.crt tls.key; do regular "$leaf_dir/$filename"; done
    cmp -s "$ca_dir/ca.crt" "$leaf_dir/ca.crt" || fail 'runtime trust certificate differs from CA'
    [ "$(mode_of "$leaf_dir/tls.key")" = 400 ] || fail 'leaf key must have mode 0400'
    [ "$(uid_of "$leaf_dir/tls.key")" = "$owner_uid" ] || fail 'leaf key owner differs from runtime UID'
    [ "$(gid_of "$leaf_dir/tls.key")" = "$owner_gid" ] || fail 'leaf key group differs from runtime GID'
    matching_key "$leaf_dir/tls.crt" "$leaf_dir/tls.key"
    subject=$(openssl x509 -in "$leaf_dir/tls.crt" -noout -subject -nameopt RFC2253 | sed 's/^subject= *//')
    [ "$subject" = "CN=$cn" ] || fail 'unexpected leaf certificate identity'
    openssl verify -CAfile "$ca_dir/ca.crt" -purpose "$purpose" "$leaf_dir/tls.crt" >/dev/null
    if [ "$role" = server ]; then
        openssl x509 -in "$leaf_dir/tls.crt" -noout -text | grep -Eq 'DNS:cortex-pg([,[:space:]]|$)' || fail 'server SAN must include cortex-pg'
    fi
}

# Check every known path before writing anything. A missing CA in an existing
# deployment must never silently create a new trust root.
present=0
for role in ca server app admin; do
    if [ "$role" = ca ]; then names='ca.crt ca.key'; else names='ca.crt tls.crt tls.key'; fi
    for name in $names; do
        path=$tls_root/$role/$name
        [ ! -L "$path" ] || fail 'PKI files must not be symlinks'
        if [ -e "$path" ]; then regular "$path"; present=$((present + 1)); fi
    done
done
if [ "$action" = init ] && [ "$present" -gt 0 ]; then
    [ "$present" -eq 11 ] || fail 'partial PKI exists; restore custody or explicitly rotate leaves'
    validate_ca
    validate_leaf server cortex-pg sslserver "$server_uid" "$server_gid"
    validate_leaf app cortex_app sslclient "$client_uid" "$client_gid"
    validate_leaf admin cortex-admin sslclient "$client_uid" "$client_gid"
    echo 'cortex TLS: existing PKI validated; no changes'
    exit 0
fi
if [ "$action" = rotate-leaves ]; then validate_ca; fi

work=$(mktemp -d "$ca_dir/.stage.XXXXXXXX")
if [ "$action" = init ]; then
    cat > "$work/ca.cnf" <<'EOF'
[req]
distinguished_name = dn
x509_extensions = v3_ca
prompt = no
[dn]
CN = Cortex standalone CA
[v3_ca]
basicConstraints = critical,CA:TRUE,pathlen:0
keyUsage = critical,keyCertSign,cRLSign
subjectKeyIdentifier = hash
authorityKeyIdentifier = keyid:always
EOF
    openssl genrsa -out "$work/ca.key" 3072 2>/dev/null
    openssl req -new -x509 -sha256 -days 3650 -key "$work/ca.key" -out "$work/ca.crt" -config "$work/ca.cnf"
    signing_cert=$work/ca.crt signing_key=$work/ca.key
else
    signing_cert=$ca_dir/ca.crt signing_key=$ca_dir/ca.key
fi

issue_leaf() {
    role=$1 cn=$2 usage=$3
    cat > "$work/$role.cnf" <<EOF
[req]
distinguished_name = dn
prompt = no
[dn]
CN = $cn
[leaf]
basicConstraints = critical,CA:FALSE
keyUsage = critical,digitalSignature,keyEncipherment
extendedKeyUsage = $usage
subjectKeyIdentifier = hash
authorityKeyIdentifier = keyid,issuer
EOF
    if [ "$role" = server ]; then echo 'subjectAltName = DNS:cortex-pg' >> "$work/$role.cnf"; fi
    openssl genrsa -out "$work/$role.key" 3072 2>/dev/null
    openssl req -new -sha256 -key "$work/$role.key" -out "$work/$role.csr" -config "$work/$role.cnf"
    serial=$(openssl rand -hex 16)
    openssl x509 -req -sha256 -days 365 -in "$work/$role.csr" -CA "$signing_cert" -CAkey "$signing_key" -set_serial "0x$serial" -out "$work/$role.crt" -extfile "$work/$role.cnf" -extensions leaf 2>/dev/null
}
issue_leaf server cortex-pg serverAuth
issue_leaf app cortex_app clientAuth
issue_leaf admin cortex-admin clientAuth

# Rotation is an explicit maintenance operation with readers stopped. Issue all
# leaves first; never overwrite the CA during rotation. An interrupted publish
# fails the next init validation and can be repaired by re-running rotate-leaves.
if [ "$action" = init ]; then
    install -m 0400 "$work/ca.key" "$ca_dir/ca.key"
    install -m 0444 "$work/ca.crt" "$ca_dir/ca.crt"
    chmod 0700 "$ca_dir"
fi
for role in server app admin; do
    if [ "$role" = server ]; then owner_uid=$server_uid owner_gid=$server_gid; else owner_uid=$client_uid owner_gid=$client_gid; fi
    chown "$owner_uid:$owner_gid" "$tls_root/$role"
    chmod 0700 "$tls_root/$role"
    install -m 0444 -o "$owner_uid" -g "$owner_gid" "$ca_dir/ca.crt" "$tls_root/$role/ca.crt"
    install -m 0444 -o "$owner_uid" -g "$owner_gid" "$work/$role.crt" "$tls_root/$role/tls.crt"
    install -m 0400 -o "$owner_uid" -g "$owner_gid" "$work/$role.key" "$tls_root/$role/tls.key"
done
validate_ca
validate_leaf server cortex-pg sslserver "$server_uid" "$server_gid"
validate_leaf app cortex_app sslclient "$client_uid" "$client_gid"
validate_leaf admin cortex-admin sslclient "$client_uid" "$client_gid"
echo "cortex TLS: $action completed; CA custody is isolated"
