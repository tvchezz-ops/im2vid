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
    status_candidates = (
        raw_response.get("status"),
        raw_response.get("data", {}).get("status"),
        raw_response.get("result", {}).get("status"),
        raw_response.get("state"),
        raw_response.get("data", {}).get("state"),
    )
    for candidate in status_candidates:
        status = str(candidate or "").strip().lower()
        if status == "created":
            return "created"
        if status in {"completed", "succeeded", "success"}:
            return "completed"
        if status in {"failed", "error", "cancelled", "canceled", "timeout"}:
            return "failed"
        if status in {"processing", "pending", "running", "in_progress", "queued", "starting"}:
            return "processing"
    if extract_output_urls(raw_response):
        return "completed"
    return "processing"


def extract_output_urls(raw_response: Dict[str, Any]) -> list[str]:
    """Извлечь output URLs из ответа Wavespeed."""
    value_candidates = (
        raw_response.get("outputs"),
        raw_response.get("data", {}).get("outputs"),
        raw_response.get("result", {}).get("outputs"),
        raw_response.get("output_urls"),
        raw_response.get("urls"),
        raw_response.get("output"),
    )
    for value in value_candidates:
        if isinstance(value, str):
            return [value] if value.strip() else []
        if isinstance(value, list):
            return [item for item in value if isinstance(item, str) and item.strip()]
        if isinstance(value, dict):
            return [item for item in value.values() if isinstance(item, str) and item.strip()]
    return []


def extract_error_message(raw_response: Dict[str, Any]) -> Optional[str]:
    """Извлечь безопасное сообщение об ошибке из ответа Wavespeed."""
    message_candidates = (
        raw_response.get("data", {}).get("error"),
        raw_response.get("data", {}).get("message"),
        raw_response.get("data", {}).get("code"),
        raw_response.get("error"),
        raw_response.get("message"),
    )
    for candidate in message_candidates:
        if candidate is None:
            continue
        sanitized = sanitize_external_error_message(str(candidate))
        if sanitized:
            return sanitized
    return None


def _sanitize_debug_value(value: Any) -> Optional[str]:
    """Подготовить безопасное значение для временного debug-лога Wavespeed."""
    if value is None:
        return None
    return sanitize_external_error_message(str(value))


def _sanitize_response_summary(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _sanitize_response_summary(child_value)
            for key, child_value in value.items()
            if str(key).lower() in {"code", "message", "error", "status", "detail", "details"}
        }
    if isinstance(value, list):
        return [_sanitize_response_summary(item) for item in value[:5]]
    if value is None or isinstance(value, (int, float, bool)):
        return value
    return sanitize_external_error_message(str(value))


def has_nsfw_contents(raw_response: Dict[str, Any]) -> bool:
    """Определить наличие NSFW-флага в ответе Wavespeed."""
    nsfw_value = raw_response.get("has_nsfw_contents")
    if isinstance(nsfw_value, bool):
        return nsfw_value
    nsfw_flags = raw_response.get("nsfw_flags")
    if isinstance(nsfw_flags, dict):
        return any(bool(value) for value in nsfw_flags.values())
    return False


def extract_execution_time(raw_response: Dict[str, Any]) -> Optional[Any]:
    """Извлечь executionTime из ответа Wavespeed без логирования остальных полей."""
    return (
        raw_response.get("executionTime")
        or raw_response.get("execution_time")
        or raw_response.get("data", {}).get("executionTime")
        or raw_response.get("data", {}).get("execution_time")
        or raw_response.get("result", {}).get("executionTime")
        or raw_response.get("result", {}).get("execution_time")
    )


class WavespeedService:
    """Сервис для интеграции с Wavespeed API."""

    BASE_URL = "https://api.wavespeed.ai"
    RESULT_PATH = "/api/v3/predictions/{prediction_id}/result"
    TERMINAL_STATUSES = {"completed", "failed"}
    MAX_RESULT_RETRIES = 3
    BASE_BACKOFF_SECONDS = 1
    COMPLETED_EMPTY_OUTPUT_RETRIES = 3
    COMPLETED_EMPTY_OUTPUT_RETRY_SECONDS = 5

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
    def _log_raw_response_debug(raw_response: Dict[str, Any]) -> None:
        """Логировать только безопасную сводку raw response без URL и payload."""
        logger.info(
            {
                "prediction_id": extract_prediction_id(raw_response),
                "status": normalize_status(raw_response),
                "outputs_count": len(extract_output_urls(raw_response)),
                "executionTime": extract_execution_time(raw_response),
            }
        )

    @staticmethod
    def _log_failed_response_debug(raw_response: Dict[str, Any]) -> None:
        """Логировать только безопасные поля failed response для временной диагностики."""
        data = raw_response.get("data", {}) if isinstance(raw_response.get("data"), dict) else {}
        logger.info(
            {
                "action": "wavespeed_failed_response_debug",
                "prediction_id": extract_prediction_id(raw_response),
                "status": normalize_status(raw_response),
                "data.error": _sanitize_debug_value(data.get("error")),
                "data.message": _sanitize_debug_value(data.get("message")),
                "data.code": _sanitize_debug_value(data.get("code")),
                "error": _sanitize_debug_value(raw_response.get("error")),
                "message": _sanitize_debug_value(raw_response.get("message")),
                "model": _sanitize_debug_value(raw_response.get("model") or data.get("model")),
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
            if response.status_code in {400, 422}:
                logger.warning(
                    {
                        "action": "wavespeed_submit_contract_error",
                        "status_code": response.status_code,
                        "model_key": model_key,
                        "endpoint": model.endpoint,
                        "payload_keys": sorted(payload),
                        "required_fields": list(model.required_payload_fields),
                        "allowed_payload_fields": list(model.allowed_payload_fields),
                        "response_body": _sanitize_response_summary(error_payload),
                    }
                )
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
                            "Генерация заняла слишком много времени. Кредит возвращён.",
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
        self._log_raw_response_debug(raw_response)
        status = normalize_status(raw_response)
        if status == "failed":
            self._log_failed_response_debug(raw_response)
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
        timeout_seconds: Optional[int] = None,
        interval: Optional[int] = None,
        generation_id: Optional[Any] = None,
    ) -> WavespeedResult:
        """Опросить Wavespeed до terminal status, отмены или таймаута с adaptive interval."""
        timeout_limit = timeout_seconds or settings.wavespeed_poll_timeout_seconds
        started = self._now()
        completed_empty_outputs_seen = 0
        while self._now() - started < timeout_limit:
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

            elapsed_seconds = int(self._now() - started)
            interval_seconds = self.get_poll_interval_seconds(elapsed_seconds, interval)
            if result.status == "completed":
                if not result.outputs:
                    completed_empty_outputs_seen += 1
                    retry_interval = self.COMPLETED_EMPTY_OUTPUT_RETRY_SECONDS
                    self._log_poll_tick(
                        generation_id=generation_id,
                        prediction_id=prediction_id,
                        elapsed_seconds=elapsed_seconds,
                        interval_seconds=retry_interval,
                        status=result.status,
                        outputs_count=len(result.outputs),
                    )
                    if completed_empty_outputs_seen > self.COMPLETED_EMPTY_OUTPUT_RETRIES:
                        return result
                    await asyncio.sleep(retry_interval)
                    continue
                self._log_poll_tick(
                    generation_id=generation_id,
                    prediction_id=prediction_id,
                    elapsed_seconds=elapsed_seconds,
                    interval_seconds=interval_seconds,
                    status=result.status,
                    outputs_count=len(result.outputs),
                )
                return result
            if result.status == "failed":
                self._log_poll_tick(
                    generation_id=generation_id,
                    prediction_id=prediction_id,
                    elapsed_seconds=elapsed_seconds,
                    interval_seconds=interval_seconds,
                    status=result.status,
                    outputs_count=len(result.outputs),
                )
                error = WavespeedFailedError(
                    result.error or "Генерация завершилась с ошибкой. Попробуйте позже.",
                    log_message="Wavespeed returned failed status",
                )
                error.result = result
                raise error

            self._log_poll_tick(
                generation_id=generation_id,
                prediction_id=prediction_id,
                elapsed_seconds=elapsed_seconds,
                interval_seconds=interval_seconds,
                status=result.status,
                outputs_count=len(result.outputs),
            )
            await asyncio.sleep(interval_seconds)

            if cancel_event is not None and cancel_event.is_set():
                raise WavespeedCancelledError(
                    "Генерация отменена.",
                    log_message="Wavespeed polling cancelled during sleep interval",
                )

        raise WavespeedTimeoutError(
            "Генерация заняла слишком много времени. Кредит возвращён.",
            log_message="Wavespeed polling timed out",
        )

    @staticmethod
    def _now() -> float:
        return asyncio.get_running_loop().time()

    @staticmethod
    def get_poll_interval_seconds(elapsed_seconds: int, fixed_interval: Optional[int] = None) -> int:
        if fixed_interval is not None:
            return fixed_interval
        if elapsed_seconds < 3 * 60:
            return settings.wavespeed_poll_fast_seconds
        if elapsed_seconds < 10 * 60:
            return settings.wavespeed_poll_normal_seconds
        return settings.wavespeed_poll_slow_seconds

    @staticmethod
    def _log_poll_tick(
        *,
        generation_id: Optional[Any],
        prediction_id: str,
        elapsed_seconds: int,
        interval_seconds: int,
        status: str,
        outputs_count: int,
    ) -> None:
        logger.info(
            {
                "action": "wavespeed_poll_tick",
                "generation_id": str(generation_id) if generation_id is not None else None,
                "prediction_id": prediction_id,
                "elapsed_seconds": elapsed_seconds,
                "interval_seconds": interval_seconds,
                "status": status,
                "outputs_count": outputs_count,
            }
        )

    @staticmethod
    def _safe_json(response: httpx.Response) -> Dict[str, Any]:
        """Преобразовать HTTP ответ в JSON-словарь без падения на не-JSON ответах."""
        try:
            data = response.json()
        except ValueError:
            return {"raw_text": response.text}
        return data if isinstance(data, dict) else {"data": data}
