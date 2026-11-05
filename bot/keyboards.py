from aiogram.types import InlineKeyboardButton as Btn, InlineKeyboardMarkup

PROVIDERS = {"hostvds": "HostVDS"}


def kb(*rows: list[Btn]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[list(r) for r in rows])


def back(to: str = "menu", text: str = "⬅️ Назад") -> list[Btn]:
    return [Btn(text=text, callback_data=to)]


def main_menu(admin: bool = False) -> InlineKeyboardMarkup:
    info_row = [Btn(text="ℹ️ Инфо", callback_data="info")]
    if admin:
        info_row.append(Btn(text="🔄 Обновить бота", callback_data="update"))
    return kb(
        [Btn(text="☁️ Мои серверы", callback_data="servers")],
        [Btn(text="👤 Профиль", callback_data="profile"), Btn(text="🔑 Аккаунты", callback_data="accounts")],
        info_row,
        [Btn(text="🛒 Купить сервер", callback_data="buy")],
    )


def grid(buttons: list[Btn], cols: int = 2) -> list[list[Btn]]:
    return [buttons[i:i + cols] for i in range(0, len(buttons), cols)]
