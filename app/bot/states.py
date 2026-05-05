"""FSM состояния для диалогов."""
from aiogram.fsm.state import State, StatesGroup


class UserStates(StatesGroup):
    """Состояния для работы с профилем пользователя."""
    
    viewing_profile = State()
    editing_name = State()


class GenerationStates(StatesGroup):
    """Состояния для генерации."""
    
    choosing_generation_type = State()
    choosing_provider = State()
    choosing_settings = State()
    choosing_setting_value = State()
    waiting_for_image = State()
    waiting_for_prompt = State()
    waiting_for_confirmation = State()
    generating = State()


class ShopStates(StatesGroup):
    """Состояния для магазина."""
    
    browsing = State()
    selecting_item = State()
    confirming_purchase = State()
