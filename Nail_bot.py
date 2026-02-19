import asyncio
import logging
import sqlite3
import re
from datetime import datetime
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder

# Настройки
BOT_TOKEN = "8532078743:AAFp3MR3_wjeUy0bw9vYCwY-m_Na1QayxSY"
ADMIN_IDS = [536841945]  # 👑 Только её ID
ZOOM_LINK = "https://zoom.us/j/123456789"  # Ссылка по умолчанию
ZOOM_PASSWORD = "123456"  # Пароль по умолчанию
ADMIN_USERNAME = "@onelona"  # Контакт администратора

# Настройка логирования
logging.basicConfig(level=logging.INFO)

# Инициализация бота
bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)


# ============================================
# БАЗА ДАННЫХ
# ============================================

class Database:
    def __init__(self):
        self.conn = None
        self.cursor = None

    def connect(self):
        """Подключение к SQLite"""
        self.conn = sqlite3.connect('zoom_bot.db', check_same_thread=False)
        self.cursor = self.conn.cursor()
        self.create_tables()
        print("✅ База данных подключена")

    def create_tables(self):
        """Создание таблиц"""
        # Таблица учеников
        self.cursor.execute("""
            CREATE TABLE IF NOT EXISTS students (
                user_id INTEGER PRIMARY KEY,
                phone TEXT UNIQUE,
                name TEXT,
                access_granted INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Таблица оплативших
        self.cursor.execute("""
            CREATE TABLE IF NOT EXISTS paid_users (
                phone TEXT PRIMARY KEY,
                name TEXT,
                course TEXT,
                paid_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Таблица для Zoom ссылок
        self.cursor.execute("""
            CREATE TABLE IF NOT EXISTS zoom_links (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                link TEXT NOT NULL,
                password TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                is_active INTEGER DEFAULT 1
            )
        """)

        # Вставляем ссылку по умолчанию
        self.cursor.execute("SELECT COUNT(*) FROM zoom_links WHERE is_active = 1")
        count = self.cursor.fetchone()[0]
        if count == 0:
            self.cursor.execute("""
                INSERT INTO zoom_links (link, password, is_active)
                VALUES (?, ?, 1)
            """, (ZOOM_LINK, ZOOM_PASSWORD))
            print("✅ Добавлена ссылка по умолчанию")

        self.conn.commit()

    def check_paid_user(self, phone):
        """Проверка оплаты"""
        self.cursor.execute("SELECT COUNT(*) FROM paid_users WHERE phone = ?", (phone,))
        count = self.cursor.fetchone()[0]
        return count > 0

    def add_student(self, user_id, phone, name):
        """Добавление ученика"""
        try:
            self.cursor.execute("""
                INSERT OR REPLACE INTO students (user_id, phone, name, access_granted)
                VALUES (?, ?, ?, 1)
            """, (user_id, phone, name))
            self.conn.commit()
            return True
        except Exception as e:
            print(f"Ошибка: {e}")
            return False

    def get_active_zoom_link(self):
        """Получение активной ссылки"""
        self.cursor.execute("""
            SELECT link, password FROM zoom_links
            WHERE is_active = 1
            ORDER BY created_at DESC
            LIMIT 1
        """)
        result = self.cursor.fetchone()
        if result:
            return {"link": result[0], "password": result[1] if result[1] else ""}
        return None

    def update_zoom_link(self, link, password=""):
        """Обновление ссылки"""
        try:
            # Деактивируем старые
            self.cursor.execute("UPDATE zoom_links SET is_active = 0")
            # Добавляем новую
            self.cursor.execute("""
                INSERT INTO zoom_links (link, password, is_active)
                VALUES (?, ?, 1)
            """, (link, password))
            self.conn.commit()
            return True
        except Exception as e:
            print(f"Ошибка: {e}")
            return False

    def close(self):
        """Закрытие"""
        if self.conn:
            self.conn.close()


# Создаем экземпляр БД
db = Database()


# ============================================
# СОСТОЯНИЯ
# ============================================

class UserStates(StatesGroup):
    waiting_phone = State()


class AdminStates(StatesGroup):
    waiting_new_link = State()
    waiting_manual_phone = State()


# ============================================
# ФИЛЬТРЫ
# ============================================

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


# ============================================
# УЧЕНИКИ
# ============================================

@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    """Старт с приветствием"""
    user_id = message.from_user.id
    user_name = message.from_user.first_name or "друг"

    # Приветственное сообщение
    welcome_text = f"👋 Привет, {user_name}!\n\n👇 Отправь свой номер телефона:"

    # Если админ - показываем админку
    if is_admin(user_id):
        await message.answer(f"👋 С возвращением, администратор!")
        await show_admin_panel(message)
        return

    # Ученик - запрос номера
    keyboard = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📱 Отправить номер", request_contact=True)]
        ],
        resize_keyboard=True,
        one_time_keyboard=True
    )

    await message.answer(welcome_text, reply_markup=keyboard)
    await state.set_state(UserStates.waiting_phone)


@dp.message(UserStates.waiting_phone, F.contact)
async def process_contact(message: types.Message, state: FSMContext):
    """Обработка контакта"""
    contact = message.contact
    phone = contact.phone_number

    # Нормализуем номер
    phone = re.sub(r'[^0-9+]', '', phone)
    if phone.startswith('8'):
        phone = '7' + phone[1:]
    if not phone.startswith('+'):
        phone = '+' + phone

    # Проверяем оплату
    if db.check_paid_user(phone):
        # Сохраняем ученика
        name = contact.first_name or "Ученик"
        db.add_student(message.from_user.id, phone, name)

        # Получаем ссылку
        zoom_data = db.get_active_zoom_link()

        if zoom_data:
            # Кнопка для перехода
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🎥 Перейти к уроку", url=zoom_data['link'])]
            ])

            # Текст сообщения
            text = "✅ **Доступ открыт!**\n\n"
            if zoom_data['password']:
                text += f"🔑 **Пароль:** `{zoom_data['password']}`\n\n"
            text += "👇 Нажмите кнопку ниже"

            await message.answer(text, parse_mode="Markdown", reply_markup=keyboard)
        else:
            await message.answer(f"❌ Нет активной ссылки. Напишите {ADMIN_USERNAME}")
    else:
        await message.answer(
            f"❌ Номер не найден.\n"
            f"Если вы оплатили, подождите или напишите {ADMIN_USERNAME}"
        )

    await state.clear()


@dp.message(UserStates.waiting_phone)
async def ignore_messages(message: types.Message, state: FSMContext):
    """Игнорируем всё кроме контакта"""
    pass


# ============================================
# АДМИН-ПАНЕЛЬ
# ============================================

async def show_admin_panel(message: types.Message):
    """Админ-панель"""
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="🔗 Текущая ссылка", callback_data="admin_current"),
        InlineKeyboardButton(text="✏️ Изменить ссылку", callback_data="admin_change")
    )
    builder.row(
        InlineKeyboardButton(text="➕ Добавить номер", callback_data="admin_add")
    )

    await message.answer(
        "👑 **Админ-панель**",
        parse_mode="Markdown",
        reply_markup=builder.as_markup()
    )


@dp.callback_query(lambda c: c.data.startswith('admin_'))
async def admin_callback(callback: types.CallbackQuery, state: FSMContext):
    """Обработка кнопок админки"""
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет прав", show_alert=True)
        return

    action = callback.data.replace('admin_', '')

    if action == 'current':
        zoom = db.get_active_zoom_link()
        if zoom:
            text = f"🔗 **Ссылка:**\n`{zoom['link']}`"
            if zoom['password']:
                text += f"\n\n🔑 **Пароль:** `{zoom['password']}`"
        else:
            text = "❌ Нет активной ссылки"

        await callback.message.answer(text, parse_mode="Markdown")
        await show_admin_panel(callback.message)

    elif action == 'change':
        await callback.message.answer(
            "✏️ Отправьте новую ссылку.\n"
            "Можно:\n"
            "- ссылку с паролем\n"
            "- ссылку и пароль через пробел\n"
            "- просто ссылку"
        )
        await state.set_state(AdminStates.waiting_new_link)

    elif action == 'add':
        await callback.message.answer(
            "➕ Введите номер в формате +79991234567:"
        )
        await state.set_state(AdminStates.waiting_manual_phone)

    await callback.answer()


@dp.message(AdminStates.waiting_new_link)
async def new_link(message: types.Message, state: FSMContext):
    """Получение новой ссылки"""
    if not is_admin(message.from_user.id):
        return

    text = message.text.strip()
    link = text
    password = ""

    # Парсим ссылку
    if " " in text and "?pwd=" not in text:
        parts = text.split(" ", 1)
        link = parts[0].strip()
        password = parts[1].strip()
    elif "?pwd=" in text:
        link = text
        password = ""

    # Сохраняем
    if db.update_zoom_link(link, password):
        await message.answer("✅ Ссылка обновлена!")
    else:
        await message.answer("❌ Ошибка")

    await state.clear()
    await show_admin_panel(message)


@dp.message(AdminStates.waiting_manual_phone)
async def manual_phone(message: types.Message, state: FSMContext):
    """Ручное добавление номера"""
    if not is_admin(message.from_user.id):
        return

    phone = message.text.strip()

    # Нормализуем номер
    phone = re.sub(r'[^0-9+]', '', phone)
    if phone.startswith('8'):
        phone = '7' + phone[1:]
    if not phone.startswith('+'):
        phone = '+' + phone

    # Проверяем формат
    if not re.match(r'^\+7[0-9]{10}$', phone):
        await message.answer("❌ Неверный формат. Нужно: +7XXXXXXXXXX")
        return

    # Добавляем в базу
    try:
        db.cursor.execute("""
            INSERT OR IGNORE INTO paid_users (phone, name)
            VALUES (?, ?)
        """, (phone, "Ручной ввод"))
        db.conn.commit()
        await message.answer(f"✅ Номер {phone} добавлен!")
    except:
        await message.answer("❌ Ошибка")

    await state.clear()
    await show_admin_panel(message)


# ============================================
# ЗАПУСК
# ============================================

async def on_startup():
    """Запуск"""
    db.connect()
    print("🚀 Бот запущен!")
    print(f"👑 Админ ID: {ADMIN_IDS[0]}")


async def on_shutdown():
    """Остановка"""
    db.close()
    await storage.close()
    await bot.close()
    print("👋 Бот остановлен")


async def main():
    await on_startup()
    await dp.start_polling(bot)


if __name__ == '__main__':
    asyncio.run(main())