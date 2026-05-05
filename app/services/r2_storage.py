"""Сервис для работы с Cloudflare R2."""
from __future__ import annotations

from importlib import import_module
from pathlib import Path
from typing import Any, Optional
import uuid

from app.config import is_r2_configured, settings
from app.utils import logger


class R2StorageService:
    """Сервис загрузки файлов в Cloudflare R2 и выдачи signed URL."""

    def __init__(self, client: Optional[Any] = None):
        self._client = client

    def is_configured(self) -> bool:
        """Проверить, что Cloudflare R2 настроен."""
        return is_r2_configured()

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        if not self.is_configured():
            raise RuntimeError("Cloudflare R2 is not configured")
        try:
            boto3 = import_module("boto3")
            botocore_config = import_module("botocore.config")
        except ImportError as exc:
            raise RuntimeError("boto3 is required for Cloudflare R2 support") from exc

        self._client = boto3.client(
            "s3",
            endpoint_url=settings.r2_endpoint_url,
            aws_access_key_id=settings.r2_access_key_id,
            aws_secret_access_key=settings.r2_secret_access_key,
            config=botocore_config.Config(signature_version="s3v4"),
        )
        return self._client

    @staticmethod
    def _normalize_filename(filename: str, local_path: str) -> str:
        raw_name = Path(filename or "").name
        if raw_name:
            return raw_name
        local_name = Path(local_path).name
        if local_name:
            return local_name
        return f"{uuid.uuid4().hex}.bin"

    def _build_object_name(self, filename: str, local_path: str) -> str:
        normalized_filename = self._normalize_filename(filename, local_path)
        return f"temporary-outputs/{uuid.uuid4().hex}/{normalized_filename}"

    def upload_file(self, local_path: str, object_name: str, content_type: str) -> str:
        """Загрузить локальный файл в R2 bucket."""
        path = Path(local_path)
        file_size = path.stat().st_size
        try:
            client = self._get_client()
            client.upload_file(
                str(path),
                settings.r2_bucket_name,
                object_name,
                ExtraArgs={"ContentType": content_type},
            )
            logger.info(
                {
                    "action": "upload_to_r2_success",
                    "delivery_method": "r2",
                    "file_size": file_size,
                    "status": "success",
                }
            )
            return object_name
        except Exception:
            logger.exception("Failed to upload file to Cloudflare R2")
            raise

    def generate_signed_url(self, object_name: str) -> str:
        """Сгенерировать signed URL для объекта в R2."""
        try:
            client = self._get_client()
            signed_url = client.generate_presigned_url(
                "get_object",
                Params={"Bucket": settings.r2_bucket_name, "Key": object_name},
                ExpiresIn=settings.r2_signed_url_ttl_seconds,
            )
            logger.info(
                {
                    "action": "signed_url_generated",
                    "delivery_method": "r2",
                    "status": "success",
                }
            )
            return signed_url
        except Exception:
            logger.exception("Failed to generate Cloudflare R2 signed URL")
            raise

    def upload_and_get_url(self, local_path: str, filename: str, content_type: str) -> str:
        """Загрузить файл в R2 и вернуть signed URL."""
        object_name = self._build_object_name(filename, local_path)
        self.upload_file(local_path, object_name, content_type)
        return self.generate_signed_url(object_name)

    def upload_and_get_object_key(self, local_path: str, filename: str, content_type: Optional[str]) -> str:
        """Загрузить файл в R2 и вернуть object key без генерации signed URL."""
        object_name = self._build_object_name(filename, local_path)
        return self.upload_file(local_path, object_name, content_type or "application/octet-stream")

    def generate_presigned_url(self, object_key: str, expires_in: Optional[int] = None) -> str:
        """Backward-compatible alias for legacy call sites."""
        if expires_in is not None:
            original_ttl = settings.r2_signed_url_ttl_seconds
            try:
                settings.r2_signed_url_ttl_seconds = expires_in
                return self.generate_signed_url(object_key)
            finally:
                settings.r2_signed_url_ttl_seconds = original_ttl
        return self.generate_signed_url(object_key)

    def upload_and_get_signed_url(
        self,
        local_path: str,
        filename: str,
        content_type: Optional[str],
    ) -> str:
        """Backward-compatible alias for legacy call sites."""
        return self.upload_and_get_url(local_path, filename, content_type or "application/octet-stream")