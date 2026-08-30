"""
security.py — Phase 1 security helpers for Taxova.ai

- Fernet encryption for files at rest (ENCRYPTION_KEY)
- PBKDF2 password hashing for CA login
- Session token helpers
- Auth context for dashboard requests
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import secrets
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Optional

from dotenv import load_dotenv

load_dotenv(override=True)

logger = logging.getLogger(__name__)

ENC_MAGIC = b"TXV1ENC1"
_PBKDF2_ITERS = 200_000


@dataclass
class AuthContext:
    """Who is calling a protected dashboard API."""

    mode: str  # "api_key" | "ca_session" | "open"
    ca_invite_code: Optional[str] = None
    ca_name: Optional[str] = None
    ca_email: Optional[str] = None
    firm_id: Optional[int] = None
    firm_name: Optional[str] = None
    is_admin_key: bool = False

    @property
    def display_name(self) -> str:
        if self.ca_name:
            return self.ca_name
        if self.is_admin_key:
            return "API Key Admin"
        return "CA Operator"

    @property
    def can_see_all_clients(self) -> bool:
        """Platform admin / open-dev only — customer firms never get this."""
        return self.is_admin_key or self.mode == "open"


_auth_ctx: ContextVar[Optional[AuthContext]] = ContextVar("taxova_auth", default=None)


def set_auth_context(ctx: AuthContext) -> None:
    _auth_ctx.set(ctx)


def get_auth_context() -> AuthContext:
    return _auth_ctx.get() or AuthContext(mode="open", is_admin_key=True)


def encryption_enabled() -> bool:
    return bool(os.getenv("ENCRYPTION_KEY", "").strip())


def _fernet():
    """Build Fernet from ENCRYPTION_KEY (url-safe base64 32-byte key, or any passphrase)."""
    raw = os.getenv("ENCRYPTION_KEY", "").strip()
    if not raw:
        return None
    try:
        from cryptography.fernet import Fernet
    except ImportError as e:
        raise RuntimeError(
            "cryptography package required for ENCRYPTION_KEY. pip install cryptography"
        ) from e

    # Accept Fernet key directly, or derive from passphrase
    try:
        return Fernet(raw.encode() if isinstance(raw, str) else raw)
    except Exception:
        digest = hashlib.sha256(raw.encode("utf-8")).digest()
        key = base64.urlsafe_b64encode(digest)
        return Fernet(key)


def generate_encryption_key() -> str:
    """Return a new Fernet key string for .env."""
    from cryptography.fernet import Fernet

    return Fernet.generate_key().decode("ascii")


def encrypt_bytes(data: bytes) -> bytes:
    """Encrypt bytes; no-op if ENCRYPTION_KEY unset. Idempotent header."""
    if not data:
        return data
    if data.startswith(ENC_MAGIC):
        return data
    f = _fernet()
    if f is None:
        return data
    return ENC_MAGIC + f.encrypt(data)


def decrypt_bytes(data: bytes) -> bytes:
    """Decrypt if magic header present; otherwise return as-is (legacy plaintext)."""
    if not data or not data.startswith(ENC_MAGIC):
        return data
    f = _fernet()
    if f is None:
        raise RuntimeError(
            "File is encrypted but ENCRYPTION_KEY is not set — cannot decrypt."
        )
    return f.decrypt(data[len(ENC_MAGIC) :])


def hash_password(password: str, salt: Optional[bytes] = None) -> tuple[str, str]:
    """Return (salt_b64, hash_b64) for storage."""
    if salt is None:
        salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, _PBKDF2_ITERS, dklen=32
    )
    return (
        base64.b64encode(salt).decode("ascii"),
        base64.b64encode(dk).decode("ascii"),
    )


def verify_password(password: str, salt_b64: str, hash_b64: str) -> bool:
    try:
        salt = base64.b64decode(salt_b64.encode("ascii"))
        expected = base64.b64decode(hash_b64.encode("ascii"))
    except Exception:
        return False
    dk = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, _PBKDF2_ITERS, dklen=32
    )
    return hmac.compare_digest(dk, expected)


def new_session_token() -> str:
    return secrets.token_urlsafe(32)


def fingerprint_token(token: str) -> str:
    """Store only a hash of the session token."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
