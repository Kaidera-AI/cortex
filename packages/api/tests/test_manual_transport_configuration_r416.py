"""Canonical trusted transport is loopback by default with an explicit override."""
from pathlib import Path
import yaml


def test_canonical_serviceauth_can_name_verified_native_peer_without_broad_default():
    root=Path(__file__).resolve().parents[3]
    compose=yaml.safe_load((root/'packages/deploy/docker-compose.yml').read_text())
    assert compose['services']['cortex-api']['environment']['CORTEX_SERVICE_AUTH_TRUSTED_NETWORKS'] == '${CORTEX_SERVICE_AUTH_TRUSTED_NETWORKS:-127.0.0.0/8,::1/128}'
