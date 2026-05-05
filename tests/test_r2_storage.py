from __future__ import annotations

import os
from pathlib import Path


os.environ.setdefault("BOT_TOKEN", "test-bot-token")
os.environ.setdefault("WAVESPEED_API_KEY", "test-api-key")
os.environ.setdefault("PUBLIC_BASE_URL", "https://example.com")


from app.services.r2_storage import R2StorageService
from app.config import settings
from app.services import r2_storage


class FakeR2Client:
    def __init__(self):
        self.upload_calls = []
        self.presign_calls = []

    def upload_file(self, local_path, bucket, object_key, ExtraArgs=None):
        self.upload_calls.append(
            {
                "local_path": local_path,
                "bucket": bucket,
                "object_key": object_key,
                "extra_args": ExtraArgs,
            }
        )

    def generate_presigned_url(self, operation_name, Params, ExpiresIn):
        self.presign_calls.append(
            {
                "operation_name": operation_name,
                "params": Params,
                "expires_in": ExpiresIn,
            }
        )
        return "https://signed.example.com/temp"


def test_is_configured_returns_true_when_required_r2_fields_are_set(monkeypatch) -> None:
    monkeypatch.setattr(settings, "r2_endpoint_url", "https://r2.example.com")
    monkeypatch.setattr(settings, "r2_access_key_id", "key-id")
    monkeypatch.setattr(settings, "r2_secret_access_key", "secret")
    monkeypatch.setattr(settings, "r2_bucket_name", "bucket")

    service = R2StorageService(client=FakeR2Client())

    assert service.is_configured() is True


def test_upload_file_logs_and_uses_expected_bucket(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(settings, "r2_bucket_name", "bucket")
    local_file = tmp_path / "image.jpg"
    local_file.write_bytes(b"image-data")
    client = FakeR2Client()
    service = R2StorageService(client=client)
    payloads = []

    def fake_info(payload):
        payloads.append(payload)

    monkeypatch.setattr(r2_storage.logger, "info", fake_info)

    object_key = service.upload_file(str(local_file), "temporary-outputs/run/image.jpg", "image/jpeg")

    assert object_key == "temporary-outputs/run/image.jpg"
    assert client.upload_calls == [
        {
            "local_path": str(local_file),
            "bucket": "bucket",
            "object_key": "temporary-outputs/run/image.jpg",
            "extra_args": {"ContentType": "image/jpeg"},
        }
    ]
    assert payloads == [
        {
            "action": "upload_to_r2_success",
            "delivery_method": "r2",
            "file_size": len(b"image-data"),
            "status": "success",
        }
    ]


def test_generate_presigned_url_uses_bucket_and_ttl(monkeypatch) -> None:
    monkeypatch.setattr(settings, "r2_bucket_name", "bucket")
    monkeypatch.setattr(settings, "r2_signed_url_ttl_seconds", 1800)
    client = FakeR2Client()
    service = R2StorageService(client=client)

    signed_url = service.generate_presigned_url("temporary-outputs/run/image.jpg", 600)

    assert signed_url == "https://signed.example.com/temp"
    assert client.presign_calls == [
        {
            "operation_name": "get_object",
            "params": {"Bucket": "bucket", "Key": "temporary-outputs/run/image.jpg"},
            "expires_in": 600,
        }
    ]


def test_upload_and_get_signed_url_uses_temporary_outputs_prefix(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(settings, "r2_bucket_name", "bucket")
    monkeypatch.setattr(settings, "r2_signed_url_ttl_seconds", 1800)
    local_file = tmp_path / "video.mp4"
    local_file.write_bytes(b"video-data")
    client = FakeR2Client()
    service = R2StorageService(client=client)

    signed_url = service.upload_and_get_signed_url(str(local_file), "wavespeed-output.mp4", "video/mp4")

    assert signed_url == "https://signed.example.com/temp"
    upload_call = client.upload_calls[0]
    assert upload_call["object_key"].startswith("temporary-outputs/")
    assert upload_call["object_key"].endswith("/wavespeed-output.mp4")
    assert client.presign_calls[0]["params"]["Key"] == upload_call["object_key"]


def test_upload_and_get_object_key_returns_generated_object_key(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(settings, "r2_bucket_name", "bucket")
    local_file = tmp_path / "video.mp4"
    local_file.write_bytes(b"video-data")
    client = FakeR2Client()
    service = R2StorageService(client=client)

    object_key = service.upload_and_get_object_key(str(local_file), "wavespeed-output.mp4", "video/mp4")

    assert object_key.startswith("temporary-outputs/")
    assert object_key.endswith("/wavespeed-output.mp4")
    assert client.upload_calls[0]["object_key"] == object_key


def test_upload_and_get_signed_url_does_not_log_signed_url(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(settings, "r2_bucket_name", "bucket")
    monkeypatch.setattr(settings, "r2_signed_url_ttl_seconds", 1800)
    local_file = tmp_path / "video.mp4"
    local_file.write_bytes(b"video-data")
    client = FakeR2Client()
    service = R2StorageService(client=client)
    payloads = []

    def fake_info(payload):
        payloads.append(payload)

    monkeypatch.setattr(r2_storage.logger, "info", fake_info)

    signed_url = service.upload_and_get_signed_url(str(local_file), "wavespeed-output.mp4", "video/mp4")

    assert signed_url == "https://signed.example.com/temp"
    assert payloads == [
        {
            "action": "upload_to_r2_success",
            "delivery_method": "r2",
            "file_size": len(b"video-data"),
            "status": "success",
        },
        {
            "action": "signed_url_generated",
            "delivery_method": "r2",
            "status": "success",
        },
    ]
    assert all(signed_url not in str(payload) for payload in payloads)