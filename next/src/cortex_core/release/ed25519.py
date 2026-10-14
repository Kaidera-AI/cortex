"""ONE Ed25519 provider boundary, installer/owner context only.

The test environment provisionally uses PyNaCl1.6.2. Production native/lock
admission remains separate. Importing this module loads no native provider.
"""
MAX_MESSAGE_BYTES=1048576


def verify_ed25519(public_key: bytes, message: bytes, signature: bytes) -> bool:
    """Strict libsodium verification; no Python curve implementation or signing.

    Bad input/signature returns False. Missing/unqualified native provider raises
    a bounded dependency error; the sole trust boundary sanitizes it. Ordinary
    Ed25519 verifies the supplied bytes; this function never uses Ed25519ph.
    """
    if (type(public_key) is not bytes or len(public_key)!=32 or type(message) is not bytes
            or len(message)>MAX_MESSAGE_BYTES or type(signature) is not bytes or len(signature)!=64):
        return False
    try:
        import nacl
        from nacl.exceptions import BadSignatureError
        from nacl.signing import VerifyKey
    except ImportError:
        raise RuntimeError('ed25519_provider_unavailable') from None
    if nacl.__version__!='1.6.2':
        raise RuntimeError('ed25519_provider_unqualified')
    try:
        return VerifyKey(public_key).verify(message,signature)==message
    except (BadSignatureError,ValueError,TypeError):
        return False
