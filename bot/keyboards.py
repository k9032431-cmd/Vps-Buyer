from aiogram.types import InlineKeyboardButton as Btn, InlineKeyboardMarkup

from .providers import PROVIDERS  # noqa: F401  (re-exported)


def kb(*rows: list[Btn]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[list(r) for r in rows])


def back(to: str = "menu", text: str = "⬅️ Назад") -> list[Btn]:
    return [Btn(text=text, callback_data=to)]


def main_menu(admin: bool = False, pending: int = 0) -> InlineKeyboardMarkup:
    rows = [
        [Btn(text="☁️ Мои серверы", callback_data="servers")],
        [Btn(text="👤 Профиль", callback_data="profile"), Btn(text="🔑 Аккаунты", callback_data="accounts")],
        [Btn(text="ℹ️ Как это работает", callback_data="info")],
    ]
    if admin:
        rows.append([Btn(text=f"👥 Пользователи{f' · 🙋 {pending}' if pending else ''}", callback_data="users"),
                     Btn(text="🔄 Обновить бота", callback_data="update")])
    rows.append([Btn(text="🛒 Купить сервер", callback_data="buy")])
    return kb(*rows)


def grid(buttons: list[Btn], cols: int = 2) -> list[list[Btn]]:
    return [buttons[i:i + cols] for i in range(0, len(buttons), cols)]
