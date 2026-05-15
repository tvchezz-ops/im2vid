"""Utilities for referral codes."""
from __future__ import annotations

import secrets
import string

REFERRAL_CODE_ALPHABET = string.ascii_letters + string.digits
DEFAULT_REFERRAL_CODE_LENGTH = 8
MAX_REFERRAL_CODE_LENGTH = 64
START_PAYLOAD_ALPHABET = string.ascii_letters + string.digits
DEFAULT_START_PAYLOAD_LENGTH = 12


def generate_referral_code(length: int = DEFAULT_REFERRAL_CODE_LENGTH) -> str:
    """Generate a short URL-safe referral code without exposing Telegram IDs."""
    if length <= 0:
        raise ValueError("Referral code length must be positive")
    if length > MAX_REFERRAL_CODE_LENGTH:
        raise ValueError(f"Referral code length must be at most {MAX_REFERRAL_CODE_LENGTH} characters")
    return "".join(secrets.choice(REFERRAL_CODE_ALPHABET) for _ in range(length))


def generate_start_payload(length: int = DEFAULT_START_PAYLOAD_LENGTH) -> str:
    """Generate an opaque URL-safe /start payload without predictable prefixes."""
    if length < 10 or length > 24:
        raise ValueError("Start payload length must be between 10 and 24 characters")
    return "".join(secrets.choice(START_PAYLOAD_ALPHABET) for _ in range(length))


def mask_start_payload(payload: str | None) -> str | None:
    """Return a log-safe payload preview."""
    if not payload:
        return None
    return f"{payload[:3]}***"
