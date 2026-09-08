#!/usr/bin/env bash
set -Eeuo pipefail
umask 077
files=(ca/ca.crt ca/ca.key server/ca.crt server/tls.crt server/tls.key
       app/ca.crt app/tls.crt app/tls.key admin/ca.crt admin/tls.crt admin/tls.key)
work=''
cleanup() {
  if [ -n "$work" ]; then
    for item in "${files[@]}"; do rm -f "$work/$item"; done
    for role in ca server app admin; do rmdir "$work/$role"; done
    rmdir "$work"
  fi
}
trap cleanup EXIT
capture_pki() {
  for role in ca server app admin; do
    [ -d "/tls/$role" ] && [ ! -L "/tls/$role" ]
  done
  for item in "${files[@]}"; do
    [ -f "/tls/$item" ] && [ ! -L "/tls/$item" ]
  done
  tar -czf "$1" -C /tls "${files[@]}"
  # keep-id maps the operator's host UID to the API/config owner, not container root.
  chown 10001:10001 "$1"
}
verify_pki() {
  work=$(mktemp -d /tmp/cortex-pki-restore.XXXXXXXX)
  for role in ca server app admin; do
    [ -d "/backup/pki/$role" ] && [ ! -L "/backup/pki/$role" ]
    mkdir -m 0700 "$work/$role"
  done
  for item in "${files[@]}"; do
    [ -f "/backup/pki/$item" ] && [ ! -L "/backup/pki/$item" ]
    case "$item" in *.key) mode=0400 ;; *) mode=0444 ;; esac
    install -m "$mode" "/backup/pki/$item" "$work/$item"
  done
  # Verify cryptographic identity in a private copy before writing any live volume.
  # Root owns this temporary tree, so no CHOWN capability is needed for verification.
  CORTEX_TLS_ROOT="$work" CORTEX_TLS_SERVER_UID=0 CORTEX_TLS_SERVER_GID=0 \
    CORTEX_TLS_CLIENT_UID=0 CORTEX_TLS_CLIENT_GID=0 \
    sh /backup-tools/tls-init.sh init
}
case "${1:-}" in
  create)
    pg_dump --format=custom --no-password --file=/backup/database.dump
    [ "$(head -c 5 /backup/database.dump)" = PGDMP ]
    chown 10001:10001 /backup/database.dump
    # CA custody is deliberately present only in this finite backup task.
    capture_pki /backup/certificates.tar.gz
    ;;
  restore)
    [ "$(head -c 5 /backup/database.dump)" = PGDMP ]
    pg_restore --exit-on-error --single-transaction --clean --if-exists \
      --no-password --dbname=platform_agent_memory /backup/database.dump
    ;;
  capture-pki)
    capture_pki /backup/previous-certificates.tar.gz
    ;;
  verify-pki)
    verify_pki
    ;;
  restore-pki)
    verify_pki
    for role in ca server app admin; do
      [ -d "/tls/$role" ] && [ ! -L "/tls/$role" ]
      case "$role" in ca) owner=0:0 ;; server) owner=999:999 ;; *) owner=10001:10001 ;; esac
      chown "$owner" "/tls/$role"
      chmod 0700 "/tls/$role"
    done
    for item in "${files[@]}"; do
      case "$item" in ca/*) owner=0:0 ;; server/*) owner=999:999 ;; *) owner=10001:10001 ;; esac
      case "$item" in *.key) mode=0400 ;; *) mode=0444 ;; esac
      [ ! -L "/tls/$item" ]
      [ ! -L "/tls/$item.restore" ]
      [ ! -e "/tls/$item.restore" ] || [ -f "/tls/$item.restore" ]
      # A stopped database never observes partially installed leaf bytes. A failed
      # publish keeps the restore journal at database-restored for an exact retry.
      install -m "$mode" -o "${owner%:*}" -g "${owner#*:}" "$work/$item" "/tls/$item.restore"
      mv -f "/tls/$item.restore" "/tls/$item"
    done
    ;;
  *) echo 'usage: backup-db.sh create|restore|capture-pki|verify-pki|restore-pki' >&2; exit 2 ;;
esac
