"""Unit tests for Wavespeed response normalization helpers."""

from __future__ import annotations

import os
import logging

import pytest

os.environ.setdefault("BOT_TOKEN", "test-bot-token")
os.environ.setdefault("WAVESPEED_API_KEY", "test-api-key")
os.environ.setdefault("PUBLIC_BASE_URL", "https://example.com")


from app.services import wavespeed
from app.services.wavespeed import WavespeedResult, WavespeedService, extract_error_message, extract_output_urls, normalize_status


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


def test_adaptive_poll_interval_defaults_to_fast_first() -> None:
    assert WavespeedService.get_poll_interval_seconds(0) == 10
    assert WavespeedService.get_poll_interval_seconds(179) == 10


def test_adaptive_poll_interval_becomes_normal_after_three_minutes() -> None:
    assert WavespeedService.get_poll_interval_seconds(180) == 30
    assert WavespeedService.get_poll_interval_seconds(599) == 30


def test_adaptive_poll_interval_becomes_slow_after_ten_minutes() -> None:
    assert WavespeedService.get_poll_interval_seconds(600) == 60


@pytest.mark.asyncio
async def test_completed_result_exits_polling_immediately(monkeypatch: pytest.MonkeyPatch) -> None:
    service = WavespeedService()
    calls = {"count": 0}

    async def fake_get_result(prediction_id: str) -> WavespeedResult:
        calls["count"] += 1
        return WavespeedResult(
            prediction_id=prediction_id,
            status="completed",
            outputs=["https://example.com/output.jpg"],
            error=None,
            has_nsfw_contents=False,
            raw_response={"status": "completed"},
        )

    async def forbidden_sleep(seconds: int) -> None:
        raise AssertionError("completed with outputs must not sleep again")

    monkeypatch.setattr(service, "get_result", fake_get_result)
    monkeypatch.setattr(wavespeed.asyncio, "sleep", forbidden_sleep)

    try:
        result = await service.poll_until_complete("pred-ready", generation_id="gen-ready")
    finally:
        await service.close()

    assert result.outputs == ["https://example.com/output.jpg"]
    assert calls["count"] == 1


@pytest.mark.asyncio
async def test_processing_first_poll_sleeps_fast_interval(monkeypatch: pytest.MonkeyPatch) -> None:
    service = WavespeedService()
    fake_time = {"seconds": 0.0}
    statuses = [
        WavespeedResult(
            prediction_id="pred-processing",
            status="processing",
            outputs=[],
            error=None,
            has_nsfw_contents=False,
            raw_response={"status": "processing"},
        ),
        WavespeedResult(
            prediction_id="pred-processing",
            status="completed",
            outputs=["https://example.com/output.jpg"],
            error=None,
            has_nsfw_contents=False,
            raw_response={"status": "completed"},
        ),
    ]
    sleep_calls: list[int] = []

    async def fake_get_result(prediction_id: str) -> WavespeedResult:
        return statuses.pop(0)

    async def fake_sleep(seconds: int) -> None:
        sleep_calls.append(seconds)
        fake_time["seconds"] += seconds

    monkeypatch.setattr(service, "get_result", fake_get_result)
    monkeypatch.setattr(service, "_now", lambda: fake_time["seconds"])
    monkeypatch.setattr(wavespeed.asyncio, "sleep", fake_sleep)

    try:
        result = await service.poll_until_complete("pred-processing", generation_id="gen-processing")
    finally:
        await service.close()

    assert result.status == "completed"
    assert sleep_calls == [10]


@pytest.mark.asyncio
async def test_completed_empty_outputs_retries_three_times_every_five_seconds(monkeypatch: pytest.MonkeyPatch) -> None:
    service = WavespeedService()
    fake_time = {"seconds": 0.0}
    calls = {"count": 0}
    sleep_calls: list[int] = []

    async def fake_get_result(prediction_id: str) -> WavespeedResult:
        calls["count"] += 1
        return WavespeedResult(
            prediction_id=prediction_id,
            status="completed",
            outputs=[],
            error=None,
            has_nsfw_contents=False,
            raw_response={"status": "completed"},
        )

    async def fake_sleep(seconds: int) -> None:
        sleep_calls.append(seconds)
        fake_time["seconds"] += seconds

    monkeypatch.setattr(service, "get_result", fake_get_result)
    monkeypatch.setattr(service, "_now", lambda: fake_time["seconds"])
    monkeypatch.setattr(wavespeed.asyncio, "sleep", fake_sleep)

    try:
        result = await service.poll_until_complete("pred-empty", generation_id="gen-empty")
    finally:
        await service.close()

    assert result.status == "completed"
    assert result.outputs == []
    assert calls["count"] == 4
    assert sleep_calls == [5, 5, 5]


@pytest.mark.asyncio
async def test_poll_tick_logs_prediction_elapsed_interval_status_and_outputs(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    service = WavespeedService()

    async def fake_get_result(prediction_id: str) -> WavespeedResult:
        return WavespeedResult(
            prediction_id=prediction_id,
            status="completed",
            outputs=["https://example.com/output.jpg"],
            error=None,
            has_nsfw_contents=False,
            raw_response={"status": "completed"},
        )

    async def forbidden_sleep(seconds: int) -> None:
        raise AssertionError("completed with outputs must not sleep")

    monkeypatch.setattr(service, "get_result", fake_get_result)
    monkeypatch.setattr(service, "_now", lambda: 0.0)
    monkeypatch.setattr(wavespeed.asyncio, "sleep", forbidden_sleep)

    try:
        with caplog.at_level(logging.INFO):
            await service.poll_until_complete("pred-log", generation_id="gen-log")
    finally:
        await service.close()

    poll_records = [
        record.msg
        for record in caplog.records
        if isinstance(record.msg, dict) and record.msg.get("action") == "wavespeed_poll_tick"
    ]
    assert poll_records[-1] == {
        "action": "wavespeed_poll_tick",
        "generation_id": "gen-log",
        "prediction_id": "pred-log",
        "elapsed_seconds": 0,
        "interval_seconds": 10,
        "status": "completed",
        "outputs_count": 1,
    }