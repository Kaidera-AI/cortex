"""ONE Ed25519 provider boundary, installer/owner context only.

The sole provider is cryptography50.0.0, shared with KOS. Production native/lock
admission remains separate. Importing this module loads no native provider.
"""
MAX_MESSAGE_BYTES=1048576
# Bounded encoded-y exclusions preserve the former strict verification contract.
# Reference: libsodium1.0.20 ge25519_has_small_order (all sign-bit variants).
# These are input encodings only; all curve/signature verification stays native.
_FIELD=2**255-19
_SMALL_ORDER_Y=frozenset((0,1,
    2707385501144840649318225287225658788936804267575313519463743609750303402022,
    55188659117513257062467267217118295137698188065244968500265048394206261417927,
    _FIELD-1))


def verify_ed25519(public_key: bytes, message: bytes, signature: bytes) -> bool:
    """Strict native Ed25519 verification; no Python curve implementation or signing.

    Bad input/signature returns False. Missing/unqualified native provider raises
    a bounded dependency error; the sole trust boundary sanitizes it. Ordinary
    Ed25519 verifies the supplied bytes; this function never uses Ed25519ph.
    """
    if (type(public_key) is not bytes or len(public_key)!=32 or type(message) is not bytes
            or len(message)>MAX_MESSAGE_BYTES or type(signature) is not bytes or len(signature)!=64):
        return False
    try:
        import cryptography
        from cryptography.exceptions import InvalidSignature,UnsupportedAlgorithm
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except ImportError:
        raise RuntimeError('ed25519_provider_unavailable') from None
    if cryptography.__version__!='50.0.0':
        raise RuntimeError('ed25519_provider_unqualified')
    for point in (public_key,signature[:32]):
        encoded_y=int.from_bytes(point,'little') & (2**255-1)
        if encoded_y>=_FIELD or encoded_y in _SMALL_ORDER_Y:
            return False
    try:
        Ed25519PublicKey.from_public_bytes(public_key).verify(signature,message)
        return True
    except (InvalidSignature,ValueError,TypeError):
        return False
    except UnsupportedAlgorithm:
        raise RuntimeError('ed25519_provider_unqualified') from None
