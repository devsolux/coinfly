"""Pure, bounded validation for the Coinbase setup form (no vault or signer).

The signer (``coinfly.security.coinbase_jwt``) reuses these patterns and the
normalizer so the UI, the vault and the JWT builder agree on one syntax.
"""

import re

KEY_NAME_PATTERN = re.compile(r"organizations/[A-Za-z0-9_-]{1,100}/apiKeys/[A-Za-z0-9_-]{1,100}\Z")
PEM_PATTERN = re.compile(r"-----BEGIN (EC PRIVATE KEY|PRIVATE KEY)-----\s+[A-Za-z0-9+/=\s]+"
                         r"-----END \1-----\s*\Z")
MAX_KEY_NAME_LENGTH = 240
MAX_SECRET_LENGTH = 4096
_KEY_NAME = KEY_NAME_PATTERN
_PEM = PEM_PATTERN


def normalize_secret(secret: str) -> str:
    """Restore multiline PEM after a single-line paste: escaped ``\\n`` or ``\\r\\n``."""
    return secret.replace("\\r\\n", "\n").replace("\\n", "\n").replace("\r\n", "\n").strip()


def credential_input_error(key: str, secret: str) -> str | None:
    if not key or not secret:
        return "CREDENTIALS_MISSING"
    if len(key) > MAX_KEY_NAME_LENGTH or KEY_NAME_PATTERN.fullmatch(key.strip()) is None:
        return "COINBASE_KEY_FORMAT_INVALID"
    if len(secret) > MAX_SECRET_LENGTH or PEM_PATTERN.fullmatch(normalize_secret(secret)) is None:
        return "COINBASE_SECRET_FORMAT_INVALID"
    return None
