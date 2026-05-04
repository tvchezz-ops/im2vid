"""Unit tests for Wavespeed response normalization helpers."""

from __future__ import annotations

import os


os.environ.setdefault("BOT_TOKEN", "test-bot-token")
os.environ.setdefault("WAVESPEED_API_KEY", "test-api-key")
os.environ.setdefault("PUBLIC_BASE_URL", "https://example.com")


from app.services.wavespeed import extract_error_message, extract_output_urls, normalize_status


def test_normalize_status_reads_nested_status_fields() -> None:
    assert normalize_status({"data": {"status": "completed"}}) == "completed"
    assert normalize_status({"result": {"status": "success"}}) == "completed"
    assert normalize_status({"data": {"state": "running"}}) == "processing"


def test_normalize_status_uses_outputs_as_completed_fallback() -> None:
    assert normalize_status({"outputs": ["https://example.com/out.png"]}) == "completed"
    assert normalize_status({"data": {"outputs": ["https://example.com/out.png"]}}) == "completed"
    assert normalize_status({"result": {"outputs": ["https://example.com/out.png"]}}) == "completed"


def test_extract_output_urls_reads_nested_output_fields() -> None:
    assert extract_output_urls({"outputs": ["https://example.com/a.png"]}) == ["https://example.com/a.png"]
    assert extract_output_urls({"data": {"outputs": ["https://example.com/b.png"]}}) == ["https://example.com/b.png"]
    assert extract_output_urls({"result": {"outputs": ["https://example.com/c.png"]}}) == ["https://example.com/c.png"]


def test_normalize_status_handles_failed_and_processing_variants() -> None:
    assert normalize_status({"status": "failed"}) == "failed"
    assert normalize_status({"status": "error"}) == "failed"
    assert normalize_status({"status": "pending"}) == "processing"
    assert normalize_status({"state": "processing"}) == "processing"


def test_extract_error_message_reads_nested_error_fields() -> None:
    assert extract_error_message({"data": {"error": "seedream moderation failed"}}) == "seedream moderation failed"
    assert extract_error_message({"data": {"message": "model input rejected"}}) == "model input rejected"
    assert extract_error_message({"data": {"code": "seedream_policy_violation"}}) == "seedream_policy_violation"
    assert extract_error_message({"error": "top-level failure"}) == "top-level failure"