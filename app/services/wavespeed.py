"""Сервис для работы с Wavespeed API."""
from __future__ import annotations

import asyncio
from typing import Any, Dict, Optional

import httpx

from app.config import settings
from app.services.generation_service import build_payload, get_generation_model
from app.utils import (
    WavespeedFailedError,
    WavespeedNetworkError,
    WavespeedTimeoutError,
    logger,
    sanitize_external_error_message,
)


class WavespeedService:
    """Сервис для интеграции с Wavespeed API."""

    BASE_URL = "https://api.wavespeed.ai"
    RESULT_PATH = "/api/v3/predictions/{prediction_id}/result"
    TERMINAL_STATUSES = {"completed", "failed"}
    MAX_RESULT_RETRIES = 3
    BASE_BACKOFF_SECONDS = 1

    def __init__(self):
        """Инициализация."""
        self.api_key = settings.wavespeed_api_key
        self.client = httpx.AsyncClient(
            base_url=self.BASE_URL,
            timeout=60.0,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
        )

    async def close(self):
        """Закрыть клиент."""
        await self.client.aclose()

    @staticmethod
    def _extract_prediction_id(payload: Dict[str, Any]) -> Optional[str]:
        """Извлечь prediction id из ответа Wavespeed."""
        return (
            payload.get("requestId")
            or payload.get("request_id")
            or payload.get("prediction_id")
            or payload.get("id")
            or payload.get("data", {}).get("requestId")
            or payload.get("data", {}).get("id")
        )

    @staticmethod
    def _normalize_status(payload: Dict[str, Any]) -> str:
        """Нормализовать статус ответа Wavespeed."""
        status = str(payload.get("status") or payload.get("state") or "").strip().lower()
        if status == "created":
            return "created"
        if status in {"processing", "running", "in_progress", "queued", "starting"}:
            return "processing"
        if status in {"completed", "succeeded", "success"}:
            return "completed"
        if status in {"failed", "error", "cancelled", "canceled", "timeout"}:
            return "failed"
        return "processing"

    @staticmethod
    def _extract_outputs(payload: Dict[str, Any]) -> list[str]:
        """Извлечь outputs из ответа Wavespeed."""
        value = payload.get("outputs") or payload.get("output_urls") or payload.get("urls") or payload.get("output") or []
        if isinstance(value, str):
            return [value]
        if isinstance(value, list):
            return [item for item in value if isinstance(item, str) and item.strip()]
        if isinstance(value, dict):
            return [item for item in value.values() if isinstance(item, str) and item.strip()]
        return []

    @staticmethod
    def _error_message(payload: Dict[str, Any]) -> str:
        """Извлечь текст ошибки из ответа Wavespeed."""
        return str(
            payload.get("error")
            or payload.get("error_message")
            or payload.get("message")
            or "Unknown Wavespeed error"
        )

    def _log_raw_response(self, action: str, payload: Dict[str, Any]) -> None:
        """Логировать только безопасную сводку ответа Wavespeed."""
        logger.info(
            "status=%s output_files=%s",
            self._normalize_status(payload),
            len(self._extract_outputs(payload)),
        )

    @staticmethod
    def get_safe_api_error_message(payload: Dict[str, Any]) -> Optional[str]:
        """Извлечь безопасное сообщение об ошибке из raw response."""
        return sanitize_external_error_message(WavespeedService._error_message(payload))

    async def submit_generation(
        self,
        model_key: str,
        images: list[str],
        prompt: str,
        options: Optional[Dict[str, Any]] = None,
    ) -> tuple[str, Dict[str, Any]]:
        """Отправить задачу генерации и вернуть prediction id и raw response."""
        model = get_generation_model(model_key)
        payload = build_payload(model_key, images, prompt)
        if options:
            payload.update({key: value for key, value in options.items() if value is not None})
        response = await self.client.post(model.endpoint, json=payload)
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            error_payload = self._safe_json(response)
            self._log_raw_response("submit error", error_payload)
            safe_message = self.get_safe_api_error_message(error_payload)
            raise WavespeedFailedError(
                safe_message or "Не удалось запустить генерацию. Попробуйте позже.",
                log_message=f"Wavespeed submit failed: {exc}",
            ) from exc

        raw_response = self._safe_json(response)
        self._log_raw_response("submit", raw_response)
        prediction_id = self._extract_prediction_id(raw_response)
        if not prediction_id:
            raise RuntimeError("Wavespeed did not return prediction id")
        return prediction_id, raw_response

    async def get_result(self, prediction_id: str) -> Dict[str, Any]:
        """Получить raw response со статусом/результатом генерации."""
        last_request_error: Optional[Exception] = None
        response: Optional[httpx.Response] = None
        for attempt in range(self.MAX_RESULT_RETRIES + 1):
            try:
                response = await self.client.get(self.RESULT_PATH.format(prediction_id=prediction_id))
                break
            except httpx.RequestError as exc:
                last_request_error = exc
                if attempt >= self.MAX_RESULT_RETRIES:
                    if isinstance(exc, httpx.TimeoutException):
                        raise WavespeedTimeoutError(
                            "Генерация заняла слишком много времени. Попробуйте позже.",
                            log_message="Wavespeed request timeout",
                        ) from exc
                    raise WavespeedNetworkError(
                        "Не удалось получить статус генерации. Попробуйте позже.",
                        log_message="Wavespeed network error",
                    ) from exc
                backoff_seconds = self.BASE_BACKOFF_SECONDS * (2 ** attempt)
                logger.warning(
                    "status=processing output_files=0 retry=%s/%s backoff_seconds=%s",
                    attempt + 1,
                    self.MAX_RESULT_RETRIES,
                    backoff_seconds,
                )
                await asyncio.sleep(backoff_seconds)

        if response is None:
            raise WavespeedNetworkError(
                "Не удалось получить статус генерации. Попробуйте позже.",
                log_message="Wavespeed response is missing",
            )
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            error_payload = self._safe_json(response)
            self._log_raw_response("result error", error_payload)
            safe_message = self.get_safe_api_error_message(error_payload)
            raise WavespeedFailedError(
                safe_message or "Сервис генерации вернул ошибку. Попробуйте позже.",
                log_message=f"Wavespeed result request failed: {exc}",
            ) from exc

        raw_response = self._safe_json(response)
        self._log_raw_response("result", raw_response)
        status = self._normalize_status(raw_response)
        outputs = self._extract_outputs(raw_response)
        if status == "completed" and not outputs:
            raise WavespeedFailedError(
                "Сервис генерации вернул пустой результат. Попробуйте позже.",
                log_message="Wavespeed completed response has empty outputs",
            )
        return raw_response

    async def poll_until_complete(
        self,
        prediction_id: str,
        timeout_seconds: int = 120,
        interval: int = 4,
    ) -> Dict[str, Any]:
        """Опросить Wavespeed до terminal status или таймаута."""
        started = asyncio.get_running_loop().time()
        while asyncio.get_running_loop().time() - started < timeout_seconds:
            raw_response = await self.get_result(prediction_id)
            status = self._normalize_status(raw_response)
            if status == "completed":
                return raw_response
            if status == "failed":
                safe_message = self.get_safe_api_error_message(raw_response)
                raise WavespeedFailedError(
                    safe_message or "Генерация завершилась с ошибкой. Попробуйте позже.",
                    log_message="Wavespeed returned failed status",
                )
            await asyncio.sleep(interval)
        raise WavespeedTimeoutError(
            "Генерация заняла слишком много времени. Попробуйте позже.",
            log_message="Wavespeed polling timed out",
        )

    @staticmethod
    def _safe_json(response: httpx.Response) -> Dict[str, Any]:
        """Преобразовать HTTP ответ в JSON-словарь без падения на не-JSON ответах."""
        try:
            data = response.json()
        except ValueError:
            return {"raw_text": response.text}
        return data if isinstance(data, dict) else {"data": data}
