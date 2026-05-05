"""Репозитории для работы с БД."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import DownloadLink, GenerationRequest, GenerationRequestStatus, Payment, User, UserEvent
from app.utils import logger


class UserRepository:
    """Репозиторий для работы с пользователями."""

    def __init__(self, session: AsyncSession):
        """Инициализация."""
        self.session = session

    async def get_or_create_user_from_telegram(self, telegram_user: Any) -> User:
        """Получить или создать пользователя из объекта Telegram."""
        user = await self.get_by_telegram_id(telegram_user.id)
        if user is None:
            user = User(id=telegram_user.id, balance=5)
            self.session.add(user)

        user.username = telegram_user.username
        user.first_name = telegram_user.first_name
        user.last_name = telegram_user.last_name
        user.language_code = telegram_user.language_code
        user.is_bot = telegram_user.is_bot
        user.is_premium = getattr(telegram_user, "is_premium", None)
        user.last_seen_at = datetime.now(timezone.utc)

        await self.session.commit()
        await self.session.refresh(user)
        logger.debug(f"User {telegram_user.id} fetched or created")
        return user

    async def update_user_seen(self, user_id: int) -> Optional[User]:
        """Обновить время последней активности пользователя."""
        user = await self.get_by_id(user_id)
        if user is None:
            return None

        user.last_seen_at = datetime.now(timezone.utc)
        await self.session.commit()
        await self.session.refresh(user)
        return user

    async def get_user_profile(self, user_id: int) -> Optional[User]:
        """Получить профиль пользователя."""
        return await self.get_by_id(user_id)

    async def get_user_delivery_preference(self, user_id: int) -> bool:
        """Получить флаг отправки результатов файлами."""
        result = await self.session.execute(
            select(User.send_results_as_files).where(User.id == user_id)
        )
        value = result.scalar_one_or_none()
        return bool(value) if value is not None else False

    async def set_user_delivery_preference(self, user_id: int, value: bool) -> bool:
        """Установить способ отправки результатов пользователя."""
        result = await self.session.execute(
            update(User)
            .where(User.id == user_id)
            .values(send_results_as_files=value)
        )
        await self.session.commit()
        return result.rowcount > 0

    async def toggle_user_delivery_preference(self, user_id: int) -> bool:
        """Переключить способ отправки результатов и вернуть новое значение."""
        current_value = await self.get_user_delivery_preference(user_id)
        await self.set_user_delivery_preference(user_id, not current_value)
        return not current_value

    async def increment_user_generation_stats(
        self,
        user_id: int,
        *,
        success: bool,
    ) -> Optional[User]:
        """Увеличить счетчики генераций пользователя."""
        user = await self.get_by_id(user_id)
        if user is None:
            return None

        user.total_generations += 1
        if success:
            user.successful_generations += 1
        else:
            user.failed_generations += 1

        await self.session.commit()
        await self.session.refresh(user)
        return user

    async def create_or_update(
        self,
        telegram_id: int,
        username: Optional[str] = None,
        first_name: Optional[str] = None,
        last_name: Optional[str] = None,
        language_code: str = "en",
    ) -> User:
        """Создать или обновить пользователя."""
        existing = await self.get_by_telegram_id(telegram_id)

        if existing is None:
            existing = User(id=telegram_id, balance=5)
            self.session.add(existing)

        existing.username = username
        existing.first_name = first_name
        existing.last_name = last_name
        existing.language_code = language_code
        existing.last_seen_at = datetime.now(timezone.utc)

        await self.session.commit()
        await self.session.refresh(existing)
        logger.debug(f"User {telegram_id} created or updated")
        return existing

    async def get_by_telegram_id(self, telegram_id: int) -> Optional[User]:
        """Получить пользователя по telegram_id."""
        result = await self.session.execute(
            select(User).where(User.id == telegram_id)
        )
        return result.scalars().first()

    async def get_by_id(self, user_id: int) -> Optional[User]:
        """Получить пользователя по id."""
        result = await self.session.execute(
            select(User).where(User.id == user_id)
        )
        return result.scalars().first()

    async def update_balance(self, telegram_id: int, amount: float) -> Optional[User]:
        """Обновить баланс пользователя."""
        user = await self.get_by_telegram_id(telegram_id)
        if user:
            user.balance += int(amount)
            await self.session.commit()
            logger.debug(f"User {telegram_id} balance updated by {amount}")
        return user

    async def has_enough_balance(self, user_id: int, amount: int) -> bool:
        """Проверить, хватает ли пользователю средств."""
        result = await self.session.execute(
            select(User.balance).where(User.id == user_id)
        )
        balance = result.scalar_one_or_none()
        return balance is not None and balance >= amount

    async def decrease_balance(self, user_id: int, amount: int) -> bool:
        """Атомарно списать средства с баланса пользователя."""
        if amount <= 0:
            raise ValueError("Decrease amount must be positive")

        result = await self.session.execute(
            update(User)
            .where(User.id == user_id, User.balance >= amount)
            .values(balance=User.balance - amount)
        )
        await self.session.commit()
        success = result.rowcount > 0
        if success:
            logger.debug("Decreased balance for user %s by %s", user_id, amount)
        return success

    async def increase_balance(self, user_id: int, amount: int) -> bool:
        """Атомарно пополнить баланс пользователя."""
        if amount <= 0:
            raise ValueError("Increase amount must be positive")

        result = await self.session.execute(
            update(User)
            .where(User.id == user_id)
            .values(balance=User.balance + amount)
        )
        await self.session.commit()
        success = result.rowcount > 0
        if success:
            logger.debug("Increased balance for user %s by %s", user_id, amount)
        return success


class GenerationRepository:
    """Репозиторий для работы с генерациями."""

    def __init__(self, session: AsyncSession):
        """Инициализация."""
        self.session = session

    async def create_generation_request(
        self,
        user_id: int,
        model_key: str,
        model_endpoint: str,
        prompt: str,
        settings: dict[str, Any] | None = None,
        input_image_file_ids: Optional[list[str]] = None,
        input_image_urls: Optional[list[str]] = None,
        aspect_ratio: Optional[str] = None,
        resolution: Optional[str] = None,
        size: Optional[str] = None,
        output_format: Optional[str] = None,
        status: str = GenerationRequestStatus.CREATED.value,
        wavespeed_prediction_id: Optional[str] = None,
        cost: int = 1,
    ) -> GenerationRequest:
        """Создать запрос на генерацию."""
        generation = GenerationRequest(
            user_id=user_id,
            model_key=model_key,
            model_endpoint=model_endpoint,
            prompt=prompt,
            settings=settings or {},
            input_image_file_ids=[],
            input_image_urls=[],
            aspect_ratio=aspect_ratio,
            resolution=resolution,
            size=size,
            output_format=output_format,
            status=GenerationRequestStatus(status),
            wavespeed_prediction_id=wavespeed_prediction_id,
            output_urls=[],
            cost=cost,
        )
        self.session.add(generation)
        await self.session.commit()
        await self.session.refresh(generation)
        logger.debug(f"Generation created for user {user_id}")
        return generation

    async def create(
        self,
        user_id: int,
        prompt: str,
        cost: float = 1.0,
    ) -> GenerationRequest:
        """Совместимость со старым интерфейсом."""
        return await self.create_generation_request(
            user_id=user_id,
            model_key="unknown",
            model_endpoint="unknown",
            prompt=prompt,
            settings={},
            cost=int(cost),
        )

    async def get_by_id(self, generation_id: Any) -> Optional[GenerationRequest]:
        """Получить генерацию по id."""
        result = await self.session.execute(
            select(GenerationRequest).where(GenerationRequest.id == generation_id)
        )
        return result.scalars().first()

    async def update_generation_status(
        self,
        generation_id: Any,
        status: str,
        *,
        nsfw_flags: Optional[dict[str, Any]] = None,
        error_message: Optional[str] = None,
        wavespeed_prediction_id: Optional[str] = None,
    ) -> Optional[GenerationRequest]:
        """Обновить статус генерации."""
        generation = await self.get_by_id(generation_id)
        if generation:
            generation.status = GenerationRequestStatus(status)
            generation.input_image_file_ids = []
            generation.input_image_urls = []
            generation.output_urls = []
            if nsfw_flags is not None:
                generation.nsfw_flags = nsfw_flags
            if error_message is not None:
                generation.error_message = error_message
            if wavespeed_prediction_id is not None:
                generation.wavespeed_prediction_id = wavespeed_prediction_id
            if generation.status == GenerationRequestStatus.COMPLETED:
                generation.completed_at = datetime.now(timezone.utc)
            await self.session.commit()
            await self.session.refresh(generation)
            logger.debug(f"Generation {generation_id} status updated to {status}")
        return generation

    async def update_status(
        self,
        generation_id: Any,
        status: str,
        result_url: Optional[str] = None,
    ) -> Optional[GenerationRequest]:
        """Совместимость со старым интерфейсом."""
        return await self.update_generation_status(
            generation_id,
            status,
        )


class EventRepository:
    """Репозиторий пользовательских событий."""

    def __init__(self, session: AsyncSession):
        """Инициализация."""
        self.session = session

    async def log_user_event(
        self,
        event_type: str,
        payload: dict[str, Any],
        user_id: Optional[int] = None,
    ) -> UserEvent:
        """Записать событие пользователя или системы."""
        event = UserEvent(user_id=user_id, event_type=event_type, payload=payload)
        self.session.add(event)
        await self.session.commit()
        await self.session.refresh(event)
        logger.debug(f"Event logged: {event_type}")
        return event


class PaymentRepository:
    """Репозиторий платежей."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def create_payment(
        self,
        user_id: int,
        amount: int,
        status: str,
        provider: Optional[str] = None,
        payload: Optional[dict[str, Any]] = None,
    ) -> Payment:
        payment = Payment(
            user_id=user_id,
            amount=amount,
            provider=provider,
            status=status,
            payload=payload or {},
        )
        self.session.add(payment)
        await self.session.commit()
        await self.session.refresh(payment)
        return payment


class DownloadLinkRepository:
    """Репозиторий коротких временных ссылок на R2-объекты."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def create_download_link(
        self,
        *,
        token: str,
        r2_object_key: str,
        filename: Optional[str],
        file_size_bytes: Optional[int],
        content_type: Optional[str],
        expires_at: datetime,
    ) -> DownloadLink:
        link = DownloadLink(
            token=token,
            r2_object_key=r2_object_key,
            filename=filename,
            file_size_bytes=file_size_bytes,
            content_type=content_type,
            expires_at=expires_at,
        )
        self.session.add(link)
        await self.session.commit()
        await self.session.refresh(link)
        return link

    async def get_by_token(self, token: str) -> Optional[DownloadLink]:
        result = await self.session.execute(select(DownloadLink).where(DownloadLink.token == token))
        return result.scalars().first()

    async def increment_used_count(self, link_id: Any) -> bool:
        result = await self.session.execute(
            update(DownloadLink)
            .where(DownloadLink.id == link_id)
            .values(used_count=DownloadLink.used_count + 1)
        )
        await self.session.commit()
        return result.rowcount > 0

    async def delete_expired_download_links(self, now: Optional[datetime] = None) -> int:
        cutoff = now or datetime.now(timezone.utc)
        result = await self.session.execute(
            delete(DownloadLink).where(DownloadLink.expires_at < cutoff)
        )
        await self.session.commit()
        return result.rowcount or 0
