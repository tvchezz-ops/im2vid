"""Сервис для работы с Wavespeed API."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Dict, Optional

import httpx

from app.config import settings
from app.services.generation_service import get_generation_model
from app.utils import (
    WavespeedCancelledError,
    WavespeedFailedError,
    WavespeedNetworkError,
    WavespeedTimeoutError,
    logger,
    sanitize_external_error_message,
)


@dataclass(frozen=True)
class WavespeedSubmitResult:
    """Результат запуска задачи в Wavespeed."""

    prediction_id: str
    status: str
    raw_response: Dict[str, Any]


@dataclass(frozen=True)
class WavespeedResult:
    """Нормализованный результат статуса/завершения генерации."""

    prediction_id: str
    status: str
    outputs: list[str]
    error: Optional[str]
    has_nsfw_contents: bool
    raw_response: Dict[str, Any]


def extract_prediction_id(raw_response: Dict[str, Any]) -> Optional[str]:
    """Извлечь prediction id из ответа Wavespeed."""
    return (
        raw_response.get("requestId")
        or raw_response.get("request_id")
        or raw_response.get("prediction_id")
        or raw_response.get("id")
        or raw_response.get("data", {}).get("requestId")
        or raw_response.get("data", {}).get("id")
    )


def normalize_status(raw_response: Dict[str, Any]) -> str:
    """Нормализовать статус ответа Wavespeed."""
    status = str(raw_response.get("status") or raw_response.get("state") or "").strip().lower()
    if status == "created":
        return "created"
    if status in {"processing", "running", "in_progress", "queued", "starting"}:
        return "processing"
    if status in {"completed", "succeeded", "success"}:
        return "completed"
    if status in {"failed", "error", "cancelled", "canceled", "timeout"}:
        return "failed"
    return "processing"


def extract_output_urls(raw_response: Dict[str, Any]) -> list[str]:
    """Извлечь output URLs из ответа Wavespeed."""
    value = raw_response.get("outputs") or raw_response.get("output_urls") or raw_response.get("urls") or raw_response.get("output") or []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str) and item.strip()]
    if isinstance(value, dict):
        return [item for item in value.values() if isinstance(item, str) and item.strip()]
    return []


def extract_error_message(raw_response: Dict[str, Any]) -> Optional[str]:
    """Извлечь безопасное сообщение об ошибке из ответа Wavespeed."""
    message = raw_response.get("error") or raw_response.get("error_message") or raw_response.get("message")
    return sanitize_external_error_message(str(message)) if message is not None else None


def has_nsfw_contents(raw_response: Dict[str, Any]) -> bool:
    """Определить наличие NSFW-флага в ответе Wavespeed."""
    nsfw_value = raw_response.get("has_nsfw_contents")
    if isinstance(nsfw_value, bool):
        return nsfw_value
    nsfw_flags = raw_response.get("nsfw_flags")
    if isinstance(nsfw_flags, dict):
        return any(bool(value) for value in nsfw_flags.values())
    return False


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

    def _log_api_event(
        self,
        *,
        action: str,
        prediction_id: Optional[str],
        status: str,
        model_key: Optional[str],
        outputs_count: int,
    ) -> None:
        """Логировать только безопасные метаданные запросов к Wavespeed."""
        logger.info(
            {
                "action": action,
                "prediction_id": prediction_id,
                "status": status,
                "model_key": model_key,
                "outputs_count": outputs_count,
            }
        )

    @staticmethod
    def get_safe_api_error_message(payload: Dict[str, Any]) -> Optional[str]:
        """Извлечь безопасное сообщение об ошибке из raw response."""
        return extract_error_message(payload)

    async def submit_generation(
        self,
        model_key: str,
        payload: Dict[str, Any],
    ) -> WavespeedSubmitResult:
        """Отправить готовый payload в Wavespeed и вернуть нормализованный результат запуска."""
        model = get_generation_model(model_key)
        response = await self.client.post(model.endpoint, json=payload)
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            error_payload = self._safe_json(response)
            self._log_api_event(
                action="wavespeed_submit_error",
                prediction_id=extract_prediction_id(error_payload),
                status=normalize_status(error_payload),
                model_key=model_key,
                outputs_count=len(extract_output_urls(error_payload)),
            )
            safe_message = self.get_safe_api_error_message(error_payload)
            raise WavespeedFailedError(
                safe_message or "Не удалось запустить генерацию. Попробуйте позже.",
                log_message=f"Wavespeed submit failed: {exc}",
            ) from exc

        raw_response = self._safe_json(response)
        prediction_id = extract_prediction_id(raw_response)
        if not prediction_id:
            raise RuntimeError("Wavespeed did not return prediction id")
        self._log_api_event(
            action="wavespeed_submit",
            prediction_id=prediction_id,
            status=normalize_status(raw_response),
            model_key=model_key,
            outputs_count=len(extract_output_urls(raw_response)),
        )
        return WavespeedSubmitResult(
            prediction_id=prediction_id,
            status=normalize_status(raw_response),
            raw_response=raw_response,
        )

    async def get_result(self, prediction_id: str) -> WavespeedResult:
        """Получить нормализованный статус/результат генерации."""
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
                self._log_api_event(
                    action="wavespeed_result_retry",
                    prediction_id=prediction_id,
                    status="processing",
                    model_key=None,
                    outputs_count=0,
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
            self._log_api_event(
                action="wavespeed_result_error",
                prediction_id=extract_prediction_id(error_payload) or prediction_id,
                status=normalize_status(error_payload),
                model_key=None,
                outputs_count=len(extract_output_urls(error_payload)),
            )
            safe_message = self.get_safe_api_error_message(error_payload)
            raise WavespeedFailedError(
                safe_message or "Сервис генерации вернул ошибку. Попробуйте позже.",
                log_message=f"Wavespeed result request failed: {exc}",
            ) from exc

        raw_response = self._safe_json(response)
        status = normalize_status(raw_response)
        outputs = extract_output_urls(raw_response)
        error_message = extract_error_message(raw_response)
        resolved_prediction_id = extract_prediction_id(raw_response) or prediction_id
        self._log_api_event(
            action="wavespeed_result",
            prediction_id=resolved_prediction_id,
            status=status,
            model_key=None,
            outputs_count=len(outputs),
        )

        if status == "completed" and not outputs:
            raise WavespeedFailedError(
                "Сервис генерации вернул пустой результат. Попробуйте позже.",
                log_message="Wavespeed completed response has empty outputs",
            )
        return WavespeedResult(
            prediction_id=resolved_prediction_id,
            status=status,
            outputs=outputs,
            error=error_message,
            has_nsfw_contents=has_nsfw_contents(raw_response),
            raw_response=raw_response,
        )

    async def poll_until_complete(
        self,
        prediction_id: str,
        cancel_event: Optional[asyncio.Event] = None,
        timeout_seconds: int = 180,
        interval: int = 4,
    ) -> WavespeedResult:
        """Опросить Wavespeed до terminal status, отмены или таймаута."""
        started = asyncio.get_running_loop().time()
        while asyncio.get_running_loop().time() - started < timeout_seconds:
            if cancel_event is not None and cancel_event.is_set():
                raise WavespeedCancelledError(
                    "Генерация отменена.",
                    log_message="Wavespeed polling cancelled before result request",
                )

            try:
                result = await self.get_result(prediction_id)
            except (WavespeedTimeoutError, WavespeedFailedError, WavespeedNetworkError):
                if cancel_event is not None and cancel_event.is_set():
                    raise WavespeedCancelledError(
                        "Генерация отменена.",
                        log_message="Wavespeed polling cancelled while handling terminal error",
                    )
                raise

            if cancel_event is not None and cancel_event.is_set():
                raise WavespeedCancelledError(
                    "Генерация отменена.",
                    log_message="Wavespeed polling cancelled after result request",
                )

            if result.status == "completed":
                return result
            if result.status == "failed":
                raise WavespeedFailedError(
                    result.error or "Генерация завершилась с ошибкой. Попробуйте позже.",
                    log_message="Wavespeed returned failed status",
                )

            await asyncio.sleep(interval)

            if cancel_event is not None and cancel_event.is_set():
                raise WavespeedCancelledError(
                    "Генерация отменена.",
                    log_message="Wavespeed polling cancelled during sleep interval",
                )

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
