"""LINE webhook signature verification."""

import base64
import hashlib
import hmac


def verify_line_signature(channel_secret: str, body: bytes, signature: str) -> bool:
    """Verify the X-Line-Signature header against the raw request body.

    LINE signs the raw body with HMAC-SHA256 using the channel secret,
    then base64-encodes the digest.
    """
    if not channel_secret or not signature:
        return False
    digest = hmac.new(channel_secret.encode("utf-8"), body, hashlib.sha256).digest()
    expected = base64.b64encode(digest).decode("utf-8")
    return hmac.compare_digest(expected, signature)
