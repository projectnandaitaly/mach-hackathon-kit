"""CAP canonicalization, signatures, grants, envelopes, and error codes."""

from cap.core.envelope import build, sign, verify
from cap.core.errors import CapError, http_status_for
from cap.core.grant import SignedGrant
from cap.core.grant import issue as issue_grant
from cap.core.grant import verify as verify_grant
from cap.core.jcs import canonicalize, card_digest, digest
from cap.core.jws import sign_detached, verify_detached
from cap.core.keys import KeyPair, PrivateKey, PublicKey, generate

__version__ = "0.9.0"

__all__ = [
    "CapError",
    "KeyPair",
    "PrivateKey",
    "PublicKey",
    "SignedGrant",
    "build",
    "canonicalize",
    "card_digest",
    "digest",
    "generate",
    "http_status_for",
    "issue_grant",
    "sign",
    "sign_detached",
    "verify",
    "verify_detached",
    "verify_grant",
]
