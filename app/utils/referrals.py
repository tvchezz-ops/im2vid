"""Utilities for referral codes."""
from __future__ import annotations

import secrets
import string

REFERRAL_CODE_ALPHABET = string.ascii_letters + string.digits
DEFAULT_REFERRAL_CODE_LENGTH = 8


def generate_referral_code(length: int = DEFAULT_REFERRAL_CODE_LENGTH) -> str:
    """Generate a short URL-safe referral code without exposing Telegram IDs."""
    if length <= 0:
        raise ValueError("Referral code length must be positive")
    return "".join(secrets.choice(REFERRAL_CODE_ALPHABET) for _ in range(length))
