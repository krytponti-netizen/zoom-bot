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
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiohttp import web

# Настройки
BOT_TOKEN = "8532078743:AAFp3MR3_wjeUy0bw9vYCwY-m_Na1QayxSY"
ADMIN_IDS = [536841945, 338097521]  # 👑 Админы
ZOOM_LINK = "https://us04web.zoom.us/j/123456789?pwd=7k9m2x4pQrA1BcDeFgHiJkLmNoPqRsTu"
ZOOM_PASSWORD = ""
ADMIN_USERNAME = "@onelona"
WEBHOOK_PORT = 8080

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Инициализация бота с глобальной защитой контента
bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(
        parse_mode=ParseMode.MARKDOWN,
        protect_content=True
    )
)
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
        logger.info("✅ База данных подключена")

    def create_tables(self):
        """Создание таблиц"""
        # Таблица учеников
        self.cursor.execute("""
            CREATE TABLE IF NOT EXISTS students (
                user_id INTEGER PRIMARY KEY,
                order_number TEXT UNIQUE,
                name TEXT,
                access_granted INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Таблица заказов (оплаченных, но ещё не использованных)
        self.cursor.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                order_number TEXT PRIMARY KEY,
                name TEXT,
                course TEXT,
                is_used INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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
            logger.info("✅ Добавлена ссылка по умолчанию")

        self.conn.commit()

    def add_order_from_site(self, order_number, name="Cliente"):
        """Добавление заказа с сайта"""
        try:
            self.cursor.execute("""
                INSERT OR IGNORE INTO orders (order_number, name, is_used)
                VALUES (?, ?, 0)
            """, (order_number, name))
            self.conn.commit()
            if self.cursor.rowcount > 0:
                logger.info(f"✅ Заказ {order_number} добавлен с сайта")
                return True
            else:
                logger.warning(f"⚠️ Заказ {order_number} уже существует")
                return False
        except Exception as e:
            logger.error(f"Ошибка добавления заказа с сайта: {e}")
            return False

    def check_order(self, order_number):
        """Проверка заказа: существует и не использован"""
        self.cursor.execute("""
            SELECT name, course FROM orders 
            WHERE order_number = ? AND is_used = 0
        """, (order_number,))
        return self.cursor.fetchone()

    def check_if_order_exists(self, order_number):
        """Проверка, существует ли заказ вообще (даже если использован)"""
        self.cursor.execute("""
            SELECT is_used FROM orders WHERE order_number = ?
        """, (order_number,))
        result = self.cursor.fetchone()
        if result:
            return result[0] == 1  # True если использован, False если нет
        return None  # Не существует

    def mark_order_as_used(self, order_number):
        """Помечает заказ как использованный"""
        self.cursor.execute("""
            UPDATE orders SET is_used = 1 WHERE order_number = ?
        """, (order_number,))
        self.conn.commit()

    def add_student(self, user_id, order_number, name):
        """Добавление ученика"""
        try:
            self.cursor.execute("""
                INSERT OR REPLACE INTO students (user_id, order_number, name, access_granted)
                VALUES (?, ?, ?, 1)
            """, (user_id, order_number, name))
            self.conn.commit()
            return True
        except Exception as e:
            logger.error(f"Ошибка: {e}")
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
            logger.error(f"Ошибка: {e}")
            return False

    def close(self):
        """Закрытие"""
        if self.conn:
            self.conn.close()


# Создаем экземпляр БД
db = Database()


# ============================================
# ВЕБХУК ДЛЯ ПРИЁМА ЗАКАЗОВ С САЙТА
# ============================================

async def handle_webhook(request):
    """Обработчик вебхука от WordPress"""
    try:
        data = await request.json()
        logger.info(f"📩 Получен вебхук: {data}")

        order_number = data.get('order_number') or data.get('order_id') or data.get('order')
        name = data.get('name') or data.get('customer_name') or "Cliente"

        if order_number:
            if db.add_order_from_site(order_number, name):
                for admin_id in ADMIN_IDS:
                    try:
                        await bot.send_message(
                            admin_id,
                            f"🛒 **Новый заказ с сайта!**\n\n"
                            f"📦 Номер: `{order_number}`\n"
                            f"👤 Имя: {name}",
                            parse_mode="Markdown"
                        )
                    except:
                        pass
                return web.Response(text="OK", status=200)
            else:
                return web.Response(text="Order already exists", status=200)
        else:
            logger.warning("❌ Не указан номер заказа в вебхуке")
            return web.Response(text="Missing order number", status=400)

    except Exception as e:
        logger.error(f"Ошибка обработки вебхука: {e}")
        return web.Response(text="Error", status=500)


async def start_webhook_server():
    """Запуск веб-сервера для вебхуков"""
    app = web.Application()
    app.router.add_post('/webhook', handle_webhook)
    app.router.add_get('/webhook', handle_webhook)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', WEBHOOK_PORT)
    await site.start()
    logger.info(f"🌐 Вебхук сервер запущен на порту {WEBHOOK_PORT}")
    logger.info(f"📬 URL для вебхука: http://твой-айпи:{WEBHOOK_PORT}/webhook")


# ============================================
# СОСТОЯНИЯ
# ============================================

class UserStates(StatesGroup):
    waiting_order = State()


class AdminStates(StatesGroup):
    waiting_new_link = State()
    waiting_order_number = State()


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
    """Старт"""
    user_id = message.from_user.id

    if is_admin(user_id):
        await message.answer(f"👋 С возвращением, администратор!")
        await show_admin_panel(message)
        return

    await message.answer(
        "👋 ¡Hola!\n\n"
        "📝 **Para obtener acceso, introduce el número de tu pedido:**\n\n"
        "🔹 El número de pedido lo recibiste después del pago\n"
        "🔹 Escríbelo en el mensaje",
        parse_mode="Markdown"
    )
    await state.set_state(UserStates.waiting_order)


@dp.message(UserStates.waiting_order)
async def process_order(message: types.Message, state: FSMContext):
    """Обработка номера заказа"""

    if not message.text:
        await message.answer(
            f"❌ Por favor, envía solo el número de pedido en formato texto.\n\n"
            f"Si necesitas ayuda, escribe a {ADMIN_USERNAME}"
        )
        return

    order = message.text.strip()
    logger.info(f"🔍 Обрабатываем заказ: '{order}'")

    order_status = db.check_if_order_exists(order)

    if order_status is None:
        await message.answer(
            f"❌ El pedido '{order}' no existe.\n\n"
            f"Verifica el número o escribe a {ADMIN_USERNAME}"
        )
        return

    if order_status is True:
        await message.answer(
            f"⚠️ El pedido '{order}' ya fue activado anteriormente.\n\n"
            f"Si crees que es un error, escribe a {ADMIN_USERNAME}"
        )
        return

    order_data = db.check_order(order)
    if order_data:
        name, course = order_data

        db.mark_order_as_used(order)
        db.add_student(message.from_user.id, order, name)

        zoom_data = db.get_active_zoom_link()

        if zoom_data and zoom_data['link'] and zoom_data['link'] != '.':
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🎥 Ir a la clase", url=zoom_data['link'])]
            ])

            text = "✅ **¡Acceso concedido!**\n\n"
            if zoom_data['password']:
                text += f"🔑 **Contraseña:** `{zoom_data['password']}`\n\n"
            text += "👇 Haz clic en el botón para entrar"

            await message.answer(text, parse_mode="Markdown", reply_markup=keyboard)
            await state.clear()
        else:
            await message.answer(
                f"❌ Lo sentimos, el enlace de Zoom no está configurado.\n"
                f"Por favor, escribe a {ADMIN_USERNAME} para obtener acceso."
            )
            await state.clear()


# ============================================
# АДМИН-ПАНЕЛЬ
# ============================================

async def show_admin_panel(message: types.Message):
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="🔗 Текущая ссылка", callback_data="admin_current"),
        InlineKeyboardButton(text="✏️ Изменить ссылку", callback_data="admin_change")
    )
    builder.row(
        InlineKeyboardButton(text="➕ Добавить заказ", callback_data="admin_add_order")
    )

    await message.answer(
        "👑 **Админ-панель**",
        parse_mode="Markdown",
        reply_markup=builder.as_markup()
    )


@dp.callback_query(lambda c: c.data.startswith('admin_'))
async def admin_callback(callback: types.CallbackQuery, state: FSMContext):
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

    elif action == 'add_order':
        await callback.message.answer(
            "➕ Введите номер заказа:"
        )
        await state.set_state(AdminStates.waiting_order_number)

    await callback.answer()


@dp.message(AdminStates.waiting_new_link)
async def new_link(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return

    text = message.text.strip()
    link = text
    password = ""

    if " " in text and "?pwd=" not in text:
        parts = text.split(" ", 1)
        link = parts[0].strip()
        password = parts[1].strip()
    elif "?pwd=" in text:
        link = text
        password = ""

    if db.update_zoom_link(link, password):
        await message.answer("✅ Ссылка обновлена!")
    else:
        await message.answer("❌ Ошибка")

    await state.clear()
    await show_admin_panel(message)


@dp.message(AdminStates.waiting_order_number)
async def add_order(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return

    order_number = message.text.strip()
    name = "Cliente"

    try:
        db.cursor.execute("""
            INSERT OR IGNORE INTO orders (order_number, name, is_used)
            VALUES (?, ?, 0)
        """, (order_number, name))
        db.conn.commit()

        if db.cursor.rowcount > 0:
            await message.answer(f"✅ Заказ {order_number} добавлен!")
        else:
            await message.answer(f"⚠️ Заказ {order_number} ya existe")
    except Exception as e:
        logger.error(f"Ошибка: {e}")
        await message.answer("❌ Ошибка при добавлении")

    await state.clear()
    await show_admin_panel(message)


# ============================================
# ЗАПУСК
# ============================================

async def on_startup():
    db.connect()
    asyncio.create_task(start_webhook_server())
    logger.info("🚀 Бот запущен!")
    logger.info(f"👑 Админы: {ADMIN_IDS}")


async def on_shutdown():
    db.close()
    await storage.close()
    await bot.close()
    logger.info("👋 Бот остановлен")


async def main():
    await on_startup()
    await dp.start_polling(bot)


if __name__ == '__main__':
    asyncio.run(main())