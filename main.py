# ==========================================================
#  ИМПОРТЫ
# ==========================================================
import asyncio
import logging
import os
import re
from datetime import datetime, timedelta
from typing import Optional

import aiohttp
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.client.session.aiohttp import AiohttpSession

from sqlalchemy import create_engine, Column, Integer, String, Boolean, DateTime, Text, ForeignKey
from sqlalchemy.orm import declarative_base, sessionmaker, relationship, Session

from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import SessionPasswordNeededError, PhoneCodeInvalidError, PhoneCodeExpiredError


# ==========================================================
#  ЛОГИРОВАНИЕ (только stdout — для Docker)
# ==========================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)


# ==========================================================
#  КОНФИГУРАЦИЯ
# ==========================================================
BOT_TOKEN = "8614324823:AAG6eUOPKWVUnlJjTav6mJfBmZOamP3M5ok"
ADMIN_ID = 906539789

TELEGRAM_API_ID = 34348645
TELEGRAM_API_HASH = "cd82b65b48126bd96a7ac64c044da62b"

CRYPTO_PAY_TOKEN = "637082:AAxmwhGkdmR8YCbsoiI0WpeQ5zKBpP7UHW7"
CRYPTO_PAY_API = "https://pay.crypt.bot/api"
CRYPTO_FIAT = "RUB"

PROXY_URL = None
# PROXY_URL = "socks5://user:pass@123.45.67.89:1080"

SBER_PHONE = "+79991321095"
SBER_NAME = "Данил Ринатович Х."

# ---------- Папки ----------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
try:
    for folder in ["data", os.path.join("data", "tdata")]:
        path = os.path.join(BASE_DIR, folder)
        os.makedirs(path, exist_ok=True)
except Exception as e:
    logging.warning(f"Не удалось создать папки (возможно read-only): {e}")


# ==========================================================
#  БАЗА ДАННЫХ
# ==========================================================
DB_PATH = os.path.join(BASE_DIR, "data", "bot.db")
engine = create_engine(f'sqlite:///{DB_PATH}', echo=False)
Base = declarative_base()
SessionLocal = sessionmaker(bind=engine)


class User(Base):
    __tablename__ = 'users'
    id = Column(Integer, primary_key=True)
    telegram_id = Column(Integer, unique=True, nullable=False)
    balance = Column(Integer, default=0)
    joined_at = Column(DateTime, default=datetime.now)
    last_activity = Column(DateTime, default=datetime.now)


class Account(Base):
    __tablename__ = 'accounts'
    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    description = Column(Text, nullable=True)
    price = Column(Integer, nullable=False)

    tdata_file_id = Column(String, nullable=True)
    tdata_text = Column(Text, nullable=True)

    phone = Column(String, nullable=True, unique=True)
    api_id = Column(Integer, nullable=True)
    api_hash = Column(String, nullable=True)
    session_string = Column(Text, nullable=True)

    is_sold = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.now)
    locked_by = Column(Integer, ForeignKey('users.id'), nullable=True)
    locked_at = Column(DateTime, nullable=True)


class PurchaseRequest(Base):
    __tablename__ = 'purchase_requests'
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False)
    account_id = Column(Integer, ForeignKey('accounts.id'), nullable=False)
    amount = Column(Integer, nullable=False)
    status = Column(String, default='pending')
    created_at = Column(DateTime, default=datetime.now)
    user = relationship('User')
    account = relationship('Account')


class DepositRequest(Base):
    __tablename__ = 'deposit_requests'
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False)
    amount = Column(Integer, nullable=False)
    tg_id_entered = Column(String, nullable=True)
    method = Column(String, default='sber')
    status = Column(String, default='pending')
    created_at = Column(DateTime, default=datetime.now)
    user = relationship('User')


class CryptoInvoice(Base):
    __tablename__ = 'crypto_invoices'
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False)
    invoice_id = Column(Integer, unique=True, nullable=False)
    amount = Column(Integer, nullable=False)
    status = Column(String, default='active')
    created_at = Column(DateTime, default=datetime.now)
    user = relationship('User')


class SupportMessage(Base):
    __tablename__ = 'support_messages'
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False)
    from_admin = Column(Boolean, default=False)
    message = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.now)
    user = relationship('User')


Base.metadata.create_all(engine)


# ==========================================================
#  ВСПОМОГАТЕЛЬНЫЕ
# ==========================================================
def get_user(session: Session, telegram_id: int) -> User:
    user = session.query(User).filter_by(telegram_id=telegram_id).first()
    if not user:
        user = User(telegram_id=telegram_id)
        session.add(user)
        session.commit()
    return user


def is_admin(telegram_id: int) -> bool:
    return telegram_id == ADMIN_ID


LOCK_TIMEOUT_SECONDS = 300


def clear_expired_locks(session: Session):
    expire_time = datetime.now() - timedelta(seconds=LOCK_TIMEOUT_SECONDS)
    expired = session.query(Account).filter(
        Account.locked_by.isnot(None),
        Account.locked_at < expire_time,
        Account.is_sold == False
    ).all()
    for acc in expired:
        acc.locked_by = None
        acc.locked_at = None
    if expired:
        session.commit()


def lock_account(session: Session, account_id: int, user_id: int) -> bool:
    clear_expired_locks(session)
    acc = session.query(Account).filter_by(id=account_id, is_sold=False).first()
    if not acc:
        return False
    if acc.locked_by and acc.locked_by != user_id:
        return False
    acc.locked_by = user_id
    acc.locked_at = datetime.now()
    session.commit()
    return True


def unlock_account(session: Session, account_id: int, user_id: int):
    acc = session.query(Account).filter_by(id=account_id).first()
    if acc and acc.locked_by == user_id:
        acc.locked_by = None
        acc.locked_at = None
        session.commit()


async def fetch_login_code(acc: Account) -> Optional[str]:
    if not (acc.session_string and acc.api_id and acc.api_hash):
        return None
    try:
        client = TelegramClient(StringSession(acc.session_string), acc.api_id, acc.api_hash)
        await client.connect()
        try:
            if not await client.is_user_authorized():
                return None
            messages = await client.get_messages(777000, limit=10)
            for msg in messages:
                text = msg.text or ""
                m = re.search(r'\b(\d{5,6})\b', text)
                if m:
                    return m.group(1)
            return None
        finally:
            await client.disconnect()
    except Exception as e:
        logging.error(f"Telethon error: {e}")
        return None


async def crypto_create_invoice(amount_rub: int, description: str) -> Optional[dict]:
    headers = {"Crypto-Pay-API-Token": CRYPTO_PAY_TOKEN}
    payload = {
        "currency_type": "fiat",
        "fiat": CRYPTO_FIAT,
        "amount": str(amount_rub),
        "description": description,
        "expires_in": 3600,
    }
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(f"{CRYPTO_PAY_API}/createInvoice", json=payload, headers=headers) as r:
                data = await r.json()
                if data.get("ok"):
                    return data["result"]
                logging.error(f"CryptoPay createInvoice: {data}")
    except Exception as e:
        logging.error(f"CryptoPay request error: {e}")
    return None


async def crypto_get_invoice(invoice_id: int) -> Optional[dict]:
    headers = {"Crypto-Pay-API-Token": CRYPTO_PAY_TOKEN}
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(f"{CRYPTO_PAY_API}/getInvoices",
                             params={"invoice_ids": str(invoice_id)},
                             headers=headers) as r:
                data = await r.json()
                if data.get("ok") and data["result"]["items"]:
                    return data["result"]["items"][0]
    except Exception as e:
        logging.error(f"CryptoPay getInvoice error: {e}")
    return None


# ==========================================================
#  КЛАВИАТУРЫ
# ==========================================================
def user_main_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👤 Профиль", callback_data="profile")],
        [InlineKeyboardButton(text="🛒 Купить аккаунт", callback_data="buy")],
        [InlineKeyboardButton(text="💰 Пополнить баланс", callback_data="deposit")],
        [InlineKeyboardButton(text="🗂 Мои покупки", callback_data="my_purchases")],
        [InlineKeyboardButton(text="💬 Поддержка", callback_data="support")]
    ])


def admin_main_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить аккаунт", callback_data="add_account")],
        [InlineKeyboardButton(text="✏️ Изменить объявление", callback_data="edit_accounts")],
        [InlineKeyboardButton(text="📋 Список аккаунтов", callback_data="list_accounts")],
        [InlineKeyboardButton(text="💬 Чат поддержки", callback_data="admin_support")],
        [InlineKeyboardButton(text="🔄 Открытые покупки", callback_data="open_purchases")],
        [InlineKeyboardButton(text="📥 Заявки на пополнение", callback_data="deposits")]
    ])


# ==========================================================
#  FSM
# ==========================================================
class AddAccountStates(StatesGroup):
    name = State()
    description = State()
    price = State()
    tdata_file = State()
    phone = State()
    code = State()
    password = State()


class EditAccountStates(StatesGroup):
    value = State()


class DepositStates(StatesGroup):
    amount = State()


class CryptoDepositStates(StatesGroup):
    amount = State()


class AdminSupportStates(StatesGroup):
    chat = State()


class SupportStates(StatesGroup):
    waiting = State()


pending_clients: dict = {}


# ==========================================================
#  ИНИЦИАЛИЗАЦИЯ
# ==========================================================
if PROXY_URL:
    session_aiogram = AiohttpSession(proxy=PROXY_URL)
    bot = Bot(token=BOT_TOKEN, session=session_aiogram)
    logging.info(f"Используется прокси: {PROXY_URL}")
else:
    bot = Bot(token=BOT_TOKEN)

dp = Dispatcher()


# ==========================================================
#  /start
# ==========================================================
@dp.message(Command("start"))
async def start_cmd(message: types.Message, state: FSMContext):
    await state.clear()
    session = SessionLocal()
    try:
        user = get_user(session, message.from_user.id)
        user.last_activity = datetime.now()
        session.commit()
    finally:
        session.close()

    if is_admin(message.from_user.id):
        await message.answer("👋 Добро пожаловать в админ-панель!", reply_markup=admin_main_keyboard())
    else:
        await message.answer("👋 Добро пожаловать! Выберите действие:", reply_markup=user_main_keyboard())


@dp.callback_query(F.data == "back_main")
async def back_main(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    if is_admin(callback.from_user.id):
        await callback.message.edit_text("Админ-панель:", reply_markup=admin_main_keyboard())
    else:
        await callback.message.edit_text("Главное меню:", reply_markup=user_main_keyboard())
    await callback.answer()


@dp.callback_query(F.data == "admin_back")
async def admin_back(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("Админ-панель:", reply_markup=admin_main_keyboard())
    await callback.answer()


# ==========================================================
#  ПРОФИЛЬ
# ==========================================================
@dp.callback_query(F.data == "profile")
async def profile(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    session = SessionLocal()
    try:
        user = get_user(session, callback.from_user.id)
        purchases = session.query(PurchaseRequest).filter_by(user_id=user.id, status='confirmed').count()
        text = (f"👤 Ваш профиль:\n"
                f"🆔 Telegram ID: {user.telegram_id}\n"
                f"💰 Баланс: {user.balance} руб.\n"
                f"🛍 Куплено аккаунтов: {purchases}\n"
                f"📅 Регистрация: {user.joined_at.strftime('%d.%m.%Y %H:%M')}")
        await callback.message.edit_text(text, reply_markup=user_main_keyboard())
    finally:
        session.close()
    await callback.answer()


# ==========================================================
#  МОИ ПОКУПКИ
# ==========================================================
@dp.callback_query(F.data == "my_purchases")
async def my_purchases(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    session = SessionLocal()
    try:
        user = get_user(session, callback.from_user.id)
        reqs = session.query(PurchaseRequest).filter_by(user_id=user.id, status='confirmed').all()
        kb_rows = []
        for r in reqs:
            acc = r.account
            if not acc:
                continue
            kb_rows.append([
                InlineKeyboardButton(text=f"{acc.name} | {acc.phone or 'без телефона'}",
                                     callback_data=f"my_acc_{acc.id}")
            ])
        if not kb_rows:
            await callback.message.edit_text("У вас пока нет купленных аккаунтов.",
                                             reply_markup=user_main_keyboard())
            await callback.answer()
            return
        kb_rows.append([InlineKeyboardButton(text="🔙 Назад", callback_data="back_main")])
        kb = InlineKeyboardMarkup(inline_keyboard=kb_rows)
        await callback.message.edit_text("Ваши купленные аккаунты:", reply_markup=kb)
    finally:
        session.close()
    await callback.answer()


@dp.callback_query(F.data.startswith("my_acc_"))
async def my_acc_view(callback: types.CallbackQuery):
    acc_id = int(callback.data.split("_")[2])
    session = SessionLocal()
    try:
        user = get_user(session, callback.from_user.id)
        req = session.query(PurchaseRequest).filter_by(
            user_id=user.id, account_id=acc_id, status='confirmed'
        ).first()
        if not req or not req.account:
            await callback.answer("Нет доступа")
            return
        acc = req.account
        text = (f"📦 {acc.name}\n"
                f"📱 Телефон: {acc.phone or '—'}\n"
                f"📝 Описание: {acc.description or '—'}\n\n"
                f"🔑 Если Telegram запросит код — нажмите «Получить код».")
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔑 Получить код", callback_data=f"get_code_{acc.id}")],
            [InlineKeyboardButton(text="📁 Получить tdata", callback_data=f"get_tdata_{acc.id}")],
            [InlineKeyboardButton(text="🔙 Назад", callback_data="my_purchases")]
        ])
        await callback.message.edit_text(text, reply_markup=kb)
    finally:
        session.close()
    await callback.answer()


@dp.callback_query(F.data.startswith("get_code_"))
async def get_code(callback: types.CallbackQuery):
    acc_id = int(callback.data.split("_")[2])
    session = SessionLocal()
    try:
        user = get_user(session, callback.from_user.id)
        req = session.query(PurchaseRequest).filter_by(
            user_id=user.id, account_id=acc_id, status='confirmed'
        ).first()
        if not req or not req.account:
            await callback.answer("Нет доступа")
            return
        acc = req.account
        await callback.answer("Запрашиваю код…")
        code = await fetch_login_code(acc)
        if code:
            await callback.message.answer(
                f"🔑 Код для входа в «{acc.name}»:\n\n`{code}`\n\n"
                f"Введите его в Telegram. Если код не подошёл — зайдите снова."
            )
        else:
            await callback.message.answer(
                "⚠️ Не удалось получить код. Возможно, сессия не активна или запрос на вход ещё не создан. "
                "Попробуйте через 10 секунд."
            )
    finally:
        session.close()


@dp.callback_query(F.data.startswith("get_tdata_"))
async def get_tdata(callback: types.CallbackQuery):
    acc_id = int(callback.data.split("_")[2])
    session = SessionLocal()
    try:
        user = get_user(session, callback.from_user.id)
        req = session.query(PurchaseRequest).filter_by(
            user_id=user.id, account_id=acc_id, status='confirmed'
        ).first()
        if not req or not req.account:
            await callback.answer("Нет доступа")
            return
        acc = req.account
        if acc.tdata_file_id:
            await callback.message.answer_document(acc.tdata_file_id,
                                                   caption=f"📁 tdata для «{acc.name}»")
        elif acc.tdata_text:
            await callback.message.answer(f"📁 tdata для «{acc.name}»:\n\n{acc.tdata_text}")
        else:
            await callback.message.answer("Для этого аккаунта tdata не загружена.")
    finally:
        session.close()
    await callback.answer()


# ==========================================================
#  ПОКУПКА
# ==========================================================
async def _render_buy_list(callback: types.CallbackQuery) -> bool:
    session = SessionLocal()
    try:
        clear_expired_locks(session)
        accounts = session.query(Account).filter_by(is_sold=False).all()
        kb_rows = []
        for acc in accounts:
            if acc.locked_by and acc.locked_by != callback.from_user.id:
                continue
            kb_rows.append([
                InlineKeyboardButton(text=f"{acc.name} - {acc.price}₽", callback_data=f"buy_{acc.id}")
            ])
        if not kb_rows:
            await callback.message.edit_text("😕 Нет доступных аккаунтов.",
                                             reply_markup=user_main_keyboard())
            return False
        kb_rows.append([InlineKeyboardButton(text="🔙 Назад", callback_data="back_main")])
        await callback.message.edit_text("Выберите аккаунт для покупки:",
                                         reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows))
        return True
    finally:
        session.close()


@dp.callback_query(F.data == "buy")
async def buy_list(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await _render_buy_list(callback)
    await callback.answer()


@dp.callback_query(F.data.startswith("buy_"))
async def buy_detail(callback: types.CallbackQuery):
    acc_id = int(callback.data.split("_")[1])
    session = SessionLocal()
    try:
        clear_expired_locks(session)
        acc = session.query(Account).filter_by(id=acc_id, is_sold=False).first()
        if not acc:
            await _render_buy_list(callback)
            await callback.answer("Аккаунт уже продан.", show_alert=True)
            return
        if not lock_account(session, acc_id, callback.from_user.id):
            await _render_buy_list(callback)
            await callback.answer("⏳ Аккаунт временно занят другим пользователем.", show_alert=True)
            return
        text = f"📌 {acc.name}\n\n{acc.description or ''}\n\n💰 Цена: {acc.price} руб."
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💳 Оплатить", callback_data=f"pay_{acc.id}")],
            [InlineKeyboardButton(text="🔙 Назад", callback_data=f"back_to_buy_{acc.id}")]
        ])
        await callback.message.edit_text(text, reply_markup=kb)
        await callback.answer()
    finally:
        session.close()


@dp.callback_query(F.data.startswith("back_to_buy_"))
async def back_from_detail(callback: types.CallbackQuery):
    acc_id = int(callback.data.split("_")[3])
    session = SessionLocal()
    try:
        unlock_account(session, acc_id, callback.from_user.id)
    finally:
        session.close()
    await _render_buy_list(callback)
    await callback.answer()


async def _deliver_account(user_tg_id: int, acc: Account):
    if acc.tdata_file_id:
        await bot.send_document(user_tg_id, acc.tdata_file_id,
                                caption=f"✅ Аккаунт «{acc.name}» — tdata во вложении")
    elif acc.tdata_text:
        await bot.send_message(user_tg_id, f"✅ Аккаунт «{acc.name}»\n\nДанные:\n{acc.tdata_text}")
    await bot.send_message(
        user_tg_id,
        f"📦 Аккаунт «{acc.name}» выдан.\n"
        f"🔑 Если Telegram запросит код — «Мои покупки» → аккаунт → «Получить код»."
    )


@dp.callback_query(F.data.startswith("pay_"))
async def pay_account(callback: types.CallbackQuery):
    acc_id = int(callback.data.split("_")[1])
    session = SessionLocal()
    try:
        user = get_user(session, callback.from_user.id)
        acc = session.query(Account).filter_by(id=acc_id, is_sold=False).first()
        if not acc:
            await _render_buy_list(callback)
            await callback.answer("Аккаунт уже продан.", show_alert=True)
            return
        unlock_account(session, acc_id, callback.from_user.id)

        if user.balance >= acc.price:
            user.balance -= acc.price
            acc.is_sold = True
            req = PurchaseRequest(user_id=user.id, account_id=acc.id, amount=acc.price, status='confirmed')
            session.add(req)
            session.commit()
            await _deliver_account(callback.from_user.id, acc)
            await callback.message.edit_text("Аккаунт куплен. Смотрите «Мои покупки».",
                                             reply_markup=user_main_keyboard())
        else:
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="💳 Я оплатил", callback_data=f"confirm_pay_{acc.id}")],
                [InlineKeyboardButton(text="🔙 Назад", callback_data="buy")]
            ])
            text = (f"❌ Недостаточно средств на балансе.\n\n"
                    f"Оплатите {acc.price} руб. по реквизитам:\n"
                    f"📱 Сбер Банк: {SBER_PHONE}\n"
                    f"👤 Получатель: {SBER_NAME}\n\n"
                    f"❗️ В комментарии укажите ваш Telegram ID: {callback.from_user.id}\n\n"
                    f"После перевода нажмите «Я оплатил».")
            await callback.message.edit_text(text, reply_markup=kb)
    finally:
        session.close()
    await callback.answer()


@dp.callback_query(F.data.startswith("confirm_pay_"))
async def confirm_pay(callback: types.CallbackQuery):
    acc_id = int(callback.data.split("_")[2])
    session = SessionLocal()
    try:
        user = get_user(session, callback.from_user.id)
        acc = session.query(Account).filter_by(id=acc_id).first()
        if not acc or acc.is_sold:
            await callback.message.edit_text("❌ Этот аккаунт уже куплен.", reply_markup=user_main_keyboard())
            await callback.answer()
            return
        req = PurchaseRequest(user_id=user.id, account_id=acc.id, amount=acc.price, status='pending')
        session.add(req)
        session.commit()
        await callback.message.edit_text("✅ Заявка создана. Ожидайте подтверждения администратора.",
                                         reply_markup=user_main_keyboard())
        await bot.send_message(ADMIN_ID,
                               f"📩 Новая заявка на покупку #{req.id}\n"
                               f"👤 Пользователь: {callback.from_user.id}\n"
                               f"📦 Аккаунт: {acc.name}\n"
                               f"💰 Сумма: {acc.price} руб.\n\n"
                               f"Реквизит Сбер: {SBER_PHONE} ({SBER_NAME})")
    finally:
        session.close()
    await callback.answer()


# ==========================================================
#  АДМИН: ОТКРЫТЫЕ ПОКУПКИ
# ==========================================================
@dp.callback_query(F.data == "open_purchases")
async def admin_open_purchases(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа")
        return
    session = SessionLocal()
    try:
        requests = session.query(PurchaseRequest).filter_by(status='pending').all()
        if not requests:
            await callback.message.edit_text("Нет открытых покупок.", reply_markup=admin_main_keyboard())
            await callback.answer()
            return
        kb_rows = []
        for req in requests:
            acc_name = req.account.name if req.account else "удалён"
            kb_rows.append([
                InlineKeyboardButton(text=f"Заявка #{req.id} | {req.user.telegram_id} | {acc_name}",
                                     callback_data=f"view_purchase_{req.id}")
            ])
        kb_rows.append([InlineKeyboardButton(text="🔙 Назад", callback_data="admin_back")])
        kb = InlineKeyboardMarkup(inline_keyboard=kb_rows)
        await callback.message.edit_text("Список заявок:", reply_markup=kb)
    finally:
        session.close()
    await callback.answer()


@dp.callback_query(F.data.startswith("view_purchase_"))
async def view_purchase(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    req_id = int(callback.data.split("_")[2])
    session = SessionLocal()
    try:
        req = session.query(PurchaseRequest).filter_by(id=req_id).first()
        if not req or req.status != 'pending':
            await callback.answer("Заявка уже обработана")
            return
        acc = req.account
        if not acc:
            await callback.message.edit_text("Аккаунт удалён из базы.", reply_markup=admin_main_keyboard())
            await callback.answer()
            return
        text = (f"📄 Заявка #{req.id}\n"
                f"👤 Пользователь: {req.user.telegram_id}\n"
                f"📦 Аккаунт: {acc.name}\n"
                f"📝 Описание: {acc.description}\n"
                f"💰 Сумма: {req.amount} руб.\n"
                f"📱 Телефон: {acc.phone or '—'}")
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Подтвердить и выдать", callback_data=f"confirm_req_{req.id}")],
            [InlineKeyboardButton(text="❌ Отменить", callback_data=f"cancel_req_{req.id}")],
            [InlineKeyboardButton(text="🔙 Назад", callback_data="open_purchases")]
        ])
        await callback.message.edit_text(text, reply_markup=kb)
    finally:
        session.close()
    await callback.answer()


@dp.callback_query(F.data.startswith("confirm_req_"))
async def confirm_purchase(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    req_id = int(callback.data.split("_")[2])
    session = SessionLocal()
    try:
        req = session.query(PurchaseRequest).filter_by(id=req_id).first()
        if not req or req.status != 'pending':
            await callback.answer("Уже обработано")
            return
        acc = req.account
        if not acc:
            req.status = 'cancelled'
            session.commit()
            await callback.message.edit_text("❌ Аккаунт удалён. Заявка отменена.",
                                             reply_markup=admin_main_keyboard())
            await callback.answer()
            return
        if acc.is_sold:
            req.status = 'cancelled'
            session.commit()
            await callback.message.edit_text("❌ Аккаунт уже продан.", reply_markup=admin_main_keyboard())
            await bot.send_message(req.user.telegram_id, "❌ Аккаунт уже продан. Свяжитесь с администратором.")
            await callback.answer()
            return
        acc.is_sold = True
        req.status = 'confirmed'
        user_tg_id = req.user.telegram_id
        session.commit()
        await _deliver_account(user_tg_id, acc)
        await callback.message.edit_text("✅ Заявка подтверждена, аккаунт выдан.",
                                         reply_markup=admin_main_keyboard())
    finally:
        session.close()
    await callback.answer()


@dp.callback_query(F.data.startswith("cancel_req_"))
async def cancel_purchase(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    req_id = int(callback.data.split("_")[2])
    session = SessionLocal()
    try:
        req = session.query(PurchaseRequest).filter_by(id=req_id).first()
        if req and req.status == 'pending':
            req.status = 'cancelled'
            session.commit()
            await callback.message.edit_text("Заявка отменена.", reply_markup=admin_main_keyboard())
    finally:
        session.close()
    await callback.answer()


# ==========================================================
#  ПОПОЛНЕНИЕ
# ==========================================================
@dp.callback_query(F.data == "deposit")
async def deposit_start(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Сбер Банк (рубли)", callback_data="dep_sber")],
        [InlineKeyboardButton(text="🪙 CryptoBot (крипта)", callback_data="dep_crypto")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="back_main")]
    ])
    await callback.message.edit_text("Выберите способ пополнения:", reply_markup=kb)
    await callback.answer()


@dp.callback_query(F.data == "dep_sber")
async def dep_sber(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.edit_text("💰 Введите сумму пополнения (от 10 до 10000 руб.):")
    await state.set_state(DepositStates.amount)
    await callback.answer()


@dp.message(DepositStates.amount, F.text)
async def dep_amount(message: types.Message, state: FSMContext):
    try:
        amount = int(message.text.strip())
        if amount < 10 or amount > 10000:
            await message.answer("Сумма должна быть от 10 до 10000 руб.")
            return
    except ValueError:
        await message.answer("Введите число.")
        return

    tg_id = str(message.from_user.id)
    text = (f"💳 Переведите {amount} руб.:\n\n"
            f"📱 Сбер Банк: {SBER_PHONE}\n"
            f"👤 Получатель: {SBER_NAME}\n\n"
            f"❗️ В комментарии укажите Telegram ID: {tg_id}\n\n"
            f"После перевода нажмите «Я оплатил».")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Я оплатил", callback_data=f"deposit_confirm_{amount}_{tg_id}")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="back_main")]
    ])
    await message.answer(text, reply_markup=kb)
    await state.clear()


@dp.callback_query(F.data.startswith("deposit_confirm_"))
async def deposit_confirm(callback: types.CallbackQuery):
    parts = callback.data.split("_")
    try:
        amount = int(parts[2])
        tg_id = parts[3]
    except (IndexError, ValueError):
        await callback.answer("Ошибка данных")
        return
    session = SessionLocal()
    try:
        user = get_user(session, callback.from_user.id)
        req = DepositRequest(user_id=user.id, amount=amount, tg_id_entered=tg_id,
                             method='sber', status='pending')
        session.add(req)
        session.commit()
        await callback.message.edit_text("✅ Заявка на пополнение создана. Ожидайте проверки.",
                                         reply_markup=user_main_keyboard())
        await bot.send_message(ADMIN_ID,
                               f"📩 Заявка на пополнение #{req.id} (Сбер)\n"
                               f"👤 Telegram: {callback.from_user.id}\n"
                               f"🆔 Указанный ID: {tg_id}\n"
                               f"💰 Сумма: {amount} руб.")
    finally:
        session.close()
    await callback.answer()


@dp.callback_query(F.data == "dep_crypto")
async def dep_crypto(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.edit_text("🪙 Введите сумму в рублях (от 10 до 10000):")
    await state.set_state(CryptoDepositStates.amount)
    await callback.answer()


@dp.message(CryptoDepositStates.amount, F.text)
async def dep_crypto_amount(message: types.Message, state: FSMContext):
    try:
        amount = int(message.text.strip())
        if amount < 10 or amount > 10000:
            await message.answer("Сумма должна быть от 10 до 10000.")
            return
    except ValueError:
        await message.answer("Введите число.")
        return

    session = SessionLocal()
    try:
        user = get_user(session, message.from_user.id)
        inv = await crypto_create_invoice(amount, f"Пополнение TG {user.telegram_id}")
        if not inv:
            await message.answer("⚠️ Не удалось создать счёт. Попробуйте позже.")
            return

        crypto_inv = CryptoInvoice(user_id=user.id, invoice_id=inv["invoice_id"],
                                   amount=amount, status='active')
        session.add(crypto_inv)
        session.commit()

        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🪙 Перейти к оплате", url=inv["bot_invoice_url"])],
            [InlineKeyboardButton(text="✅ Я оплатил", callback_data=f"crypto_check_{inv['invoice_id']}")],
            [InlineKeyboardButton(text="🔙 Назад", callback_data="back_main")]
        ])
        asset = inv.get('asset') or 'USDT'
        await message.answer(
            f"🪙 Счёт на {amount} руб.\nК оплате: {inv.get('amount')} {asset}",
            reply_markup=kb
        )
    finally:
        session.close()
        await state.clear()


@dp.callback_query(F.data.startswith("crypto_check_"))
async def crypto_check(callback: types.CallbackQuery):
    invoice_id = int(callback.data.split("_")[2])
    session = SessionLocal()
    try:
        inv_row = session.query(CryptoInvoice).filter_by(invoice_id=invoice_id).first()
        if not inv_row:
            await callback.answer("Счёт не найден")
            return
        if inv_row.status == 'paid':
            await callback.answer("Уже оплачен")
            return

        info = await crypto_get_invoice(invoice_id)
        if not info or info.get("status") != "paid":
            await callback.answer("⏳ Оплата ещё не получена. Попробуйте через минуту.")
            return

        updated = session.query(CryptoInvoice).filter_by(
            id=inv_row.id, status='active'
        ).update({'status': 'paid'})
        if not updated:
            session.rollback()
            await callback.answer("Уже обработано")
            return

        user = inv_row.user
        user.balance += inv_row.amount
        dep = DepositRequest(user_id=user.id, amount=inv_row.amount,
                             tg_id_entered=str(user.telegram_id),
                             method='crypto', status='confirmed')
        session.add(dep)
        session.commit()

        await callback.message.edit_text(
            f"✅ Оплата получена! Баланс пополнен на {inv_row.amount} руб.",
            reply_markup=user_main_keyboard()
        )
        await bot.send_message(ADMIN_ID, f"🪙 Крипто-пополнение\n👤 {user.telegram_id}\n💰 {inv_row.amount} руб.")
    finally:
        session.close()


# ==========================================================
#  АДМИН: ЗАЯВКИ НА ПОПОЛНЕНИЕ
# ==========================================================
@dp.callback_query(F.data == "deposits")
async def admin_deposits(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    if not is_admin(callback.from_user.id):
        return
    session = SessionLocal()
    try:
        reqs = session.query(DepositRequest).filter_by(status='pending').all()
        if not reqs:
            await callback.message.edit_text("Нет заявок на пополнение.", reply_markup=admin_main_keyboard())
            await callback.answer()
            return
        kb_rows = []
        for req in reqs:
            method_label = "Сбер" if req.method == 'sber' else "Crypto"
            kb_rows.append([
                InlineKeyboardButton(
                    text=f"#{req.id} | {method_label} | {req.user.telegram_id} | {req.amount}₽",
                    callback_data=f"view_deposit_{req.id}")
            ])
        kb_rows.append([InlineKeyboardButton(text="🔙 Назад", callback_data="admin_back")])
        kb = InlineKeyboardMarkup(inline_keyboard=kb_rows)
        await callback.message.edit_text("Заявки на пополнение:", reply_markup=kb)
    finally:
        session.close()
    await callback.answer()


@dp.callback_query(F.data.startswith("view_deposit_"))
async def view_deposit(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    req_id = int(callback.data.split("_")[2])
    session = SessionLocal()
    try:
        req = session.query(DepositRequest).filter_by(id=req_id).first()
        if not req or req.status != 'pending':
            await callback.answer("Уже обработано")
            return
        text = (f"📄 Заявка #{req.id}\n"
                f"💳 Способ: {'Сбер' if req.method == 'sber' else 'CryptoBot'}\n"
                f"👤 Telegram: {req.user.telegram_id}\n"
                f"🆔 Указанный ID: {req.tg_id_entered or '—'}\n"
                f"💰 Сумма: {req.amount} руб.")
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"confirm_dep_{req.id}")],
            [InlineKeyboardButton(text="❌ Отменить", callback_data=f"cancel_dep_{req.id}")],
            [InlineKeyboardButton(text="🔙 Назад", callback_data="deposits")]
        ])
        await callback.message.edit_text(text, reply_markup=kb)
    finally:
        session.close()
    await callback.answer()


@dp.callback_query(F.data.startswith("confirm_dep_"))
async def confirm_deposit(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    req_id = int(callback.data.split("_")[2])
    session = SessionLocal()
    try:
        req = session.query(DepositRequest).filter_by(id=req_id).first()
        if not req or req.status != 'pending':
            await callback.answer("Уже обработано")
            return
        req.user.balance += req.amount
        req.status = 'confirmed'
        user_tg_id = req.user.telegram_id
        amount = req.amount
        session.commit()
        await bot.send_message(user_tg_id, f"💰 Ваш баланс пополнен на {amount} руб.")
        await callback.message.edit_text("✅ Подтверждено.", reply_markup=admin_main_keyboard())
    finally:
        session.close()
    await callback.answer()


@dp.callback_query(F.data.startswith("cancel_dep_"))
async def cancel_deposit(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    req_id = int(callback.data.split("_")[2])
    session = SessionLocal()
    try:
        req = session.query(DepositRequest).filter_by(id=req_id).first()
        if req and req.status == 'pending':
            req.status = 'cancelled'
            session.commit()
            await callback.message.edit_text("Заявка отменена.", reply_markup=admin_main_keyboard())
    finally:
        session.close()
    await callback.answer()


# ==========================================================
#  АДМИН: ДОБАВЛЕНИЕ АККАУНТА
# ==========================================================
@dp.callback_query(F.data == "add_account")
async def add_account(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    if not is_admin(callback.from_user.id):
        return
    if not TELEGRAM_API_ID or not TELEGRAM_API_HASH:
        await callback.message.edit_text(
            "⚠️ В боте не заданы TELEGRAM_API_ID / TELEGRAM_API_HASH.\n"
            "Впишите их в начало файла и перезапустите бота.",
            reply_markup=admin_main_keyboard()
        )
        await callback.answer()
        return
    await callback.message.edit_text("Введите название аккаунта:")
    await state.set_state(AddAccountStates.name)
    await callback.answer()


@dp.message(AddAccountStates.name, F.text)
async def add_name(message: types.Message, state: FSMContext):
    await state.update_data(name=message.text.strip())
    await message.answer("Введите описание аккаунта:")
    await state.set_state(AddAccountStates.description)


@dp.message(AddAccountStates.description, F.text)
async def add_desc(message: types.Message, state: FSMContext):
    await state.update_data(description=message.text.strip())
    await message.answer("Введите цену (только число, в рублях):")
    await state.set_state(AddAccountStates.price)


@dp.message(AddAccountStates.price, F.text)
async def add_price(message: types.Message, state: FSMContext):
    try:
        price = int(message.text.strip())
        if price <= 0:
            raise ValueError
    except ValueError:
        await message.answer("Цена должна быть положительным числом.")
        return
    await state.update_data(price=price)
    await message.answer("📁 Отправьте архив tdata файлом (документом).\n"
                         "Можно пропустить — отправьте «-».")
    await state.set_state(AddAccountStates.tdata_file)


@dp.message(AddAccountStates.tdata_file, F.document)
async def add_tdata_doc(message: types.Message, state: FSMContext):
    await state.update_data(tdata_file_id=message.document.file_id, tdata_text=None)
    await message.answer("✅ tdata сохранена.\n\n"
                         "Введите телефон аккаунта в формате +79990001122:")
    await state.set_state(AddAccountStates.phone)


@dp.message(AddAccountStates.tdata_file, F.text)
async def add_tdata_text(message: types.Message, state: FSMContext):
    v = message.text.strip()
    if v == '-':
        await state.update_data(tdata_file_id=None, tdata_text=None)
    else:
        await state.update_data(tdata_file_id=None, tdata_text=v)
    await message.answer("Введите телефон аккаунта в формате +79990001122:")
    await state.set_state(AddAccountStates.phone)


@dp.message(AddAccountStates.phone, F.text)
async def add_phone(message: types.Message, state: FSMContext):
    phone = message.text.strip()
    if not re.match(r'^\+\d{7,15}$', phone):
        await message.answer("Телефон должен быть в формате +79990001122. Попробуйте снова.")
        return

    session = SessionLocal()
    try:
        existing = session.query(Account).filter_by(phone=phone).first()
        if existing:
            await message.answer(f"⚠️ Аккаунт с номером {phone} уже есть в базе "
                                 f"(ID {existing.id}, «{existing.name}»). Начните заново: /start")
            await state.clear()
            return
    finally:
        session.close()

    await state.update_data(phone=phone)

    old_client = pending_clients.pop(message.from_user.id, None)
    if old_client:
        try:
            await old_client.disconnect()
        except Exception:
            pass

    client = None
    try:
        client = TelegramClient(StringSession(), TELEGRAM_API_ID, TELEGRAM_API_HASH)
        await client.connect()
        sent = await client.send_code_request(phone)
        pending_clients[message.from_user.id] = client
        await state.update_data(phone_code_hash=sent.phone_code_hash)
        await message.answer("📩 Код отправлен в Telegram/SMS на этот номер.\n"
                             "Введите код (только цифры):")
        await state.set_state(AddAccountStates.code)
    except Exception as e:
        logging.exception("Ошибка отправки кода")
        if client:
            try:
                await client.disconnect()
            except Exception:
                pass
        pending_clients.pop(message.from_user.id, None)
        await message.answer(f"⚠️ Не удалось отправить код: {e}\nНачните заново: /start")
        await state.clear()


@dp.message(AddAccountStates.code, F.text)
async def add_code(message: types.Message, state: FSMContext):
    data = await state.get_data()
    client = pending_clients.get(message.from_user.id)
    if not client:
        await message.answer("Сессия потеряна. Начните заново: /start")
        await state.clear()
        return
    code = message.text.strip().replace(" ", "")
    try:
        await client.sign_in(data['phone'], code, phone_code_hash=data['phone_code_hash'])
    except SessionPasswordNeededError:
        await message.answer("🔐 Включена двухэтапная аутентификация. Введите пароль 2FA:")
        await state.set_state(AddAccountStates.password)
        return
    except PhoneCodeInvalidError:
        await message.answer("❌ Неверный код. Попробуйте снова.")
        return
    except PhoneCodeExpiredError:
        await message.answer("⌛ Код истёк. Начните заново: /start")
        await state.clear()
        try:
            await client.disconnect()
        except Exception:
            pass
        pending_clients.pop(message.from_user.id, None)
        return
    except Exception as e:
        await message.answer(f"⚠️ Ошибка: {e}")
        return

    await _finalize_account(message, state, client, data)


@dp.message(AddAccountStates.password, F.text)
async def add_password(message: types.Message, state: FSMContext):
    data = await state.get_data()
    client = pending_clients.get(message.from_user.id)
    if not client:
        await message.answer("Сессия потеряна. Начните заново: /start")
        await state.clear()
        return
    try:
        await client.sign_in(password=message.text.strip())
    except Exception as e:
        await message.answer(f"❌ Неверный пароль или ошибка: {e}")
        return
    await _finalize_account(message, state, client, data)


async def _finalize_account(message: types.Message, state: FSMContext, client: TelegramClient, data: dict):
    try:
        session_string = client.session.save()
    except Exception as e:
        await message.answer(f"Не удалось сохранить сессию: {e}")
        try:
            await client.disconnect()
        except Exception:
            pass
        pending_clients.pop(message.from_user.id, None)
        await state.clear()
        return

    try:
        me = await client.get_me()
        logging.info(f"Успешный вход как {me}")
    except Exception:
        pass

    try:
        await client.disconnect()
    except Exception:
        pass
    pending_clients.pop(message.from_user.id, None)

    session = SessionLocal()
    try:
        acc = Account(
            name=data['name'],
            description=data['description'],
            price=data['price'],
            tdata_file_id=data.get('tdata_file_id'),
            tdata_text=data.get('tdata_text'),
            phone=data['phone'],
            api_id=TELEGRAM_API_ID,
            api_hash=TELEGRAM_API_HASH,
            session_string=session_string,
        )
        session.add(acc)
        session.commit()
    finally:
        session.close()

    await message.answer(
        f"✅ Аккаунт «{data['name']}» добавлен и авторизован!\n"
        f"Теперь бот сможет выдавать коды покупателям.",
        reply_markup=admin_main_keyboard()
    )
    await state.clear()


# ==========================================================
#  АДМИН: СПИСОК АККАУНТОВ
# ==========================================================
@dp.callback_query(F.data == "list_accounts")
async def list_accounts(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    if not is_admin(callback.from_user.id):
        return
    session = SessionLocal()
    try:
        accounts = session.query(Account).all()
        if not accounts:
            await callback.message.edit_text("В базе нет аккаунтов.", reply_markup=admin_main_keyboard())
            await callback.answer()
            return
        header = "📋 Список всех аккаунтов:\n\n"
        lines = []
        for acc in accounts:
            status = "🟢" if not acc.is_sold else "🔴"
            tdata = "📁" if acc.tdata_file_id else ("📝" if acc.tdata_text else "—")
            auth = "🔐" if acc.session_string else "⚠️"
            lines.append(f"{status} ID:{acc.id} | {acc.name} | {acc.price}₽ | tdata:{tdata} | session:{auth}")

        chunks = []
        current = header
        for line in lines:
            if len(current) + len(line) + 1 > 3500:
                chunks.append(current)
                current = ""
            current += line + "\n"
        if current.strip():
            chunks.append(current)

        await callback.message.edit_text(chunks[0], reply_markup=admin_main_keyboard())
        for chunk in chunks[1:]:
            await callback.message.answer(chunk)
    finally:
        session.close()
    await callback.answer()


# ==========================================================
#  АДМИН: РЕДАКТИРОВАНИЕ
# ==========================================================
@dp.callback_query(F.data == "edit_accounts")
async def edit_accounts(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    if not is_admin(callback.from_user.id):
        return
    session = SessionLocal()
    try:
        accounts = session.query(Account).all()
        if not accounts:
            await callback.message.edit_text("Нет аккаунтов.", reply_markup=admin_main_keyboard())
            await callback.answer()
            return
        kb_rows = []
        for acc in accounts:
            kb_rows.append([
                InlineKeyboardButton(text=f"{acc.id}. {acc.name} ({acc.price}₽)",
                                     callback_data=f"edit_select_{acc.id}")
            ])
        kb_rows.append([InlineKeyboardButton(text="🔙 Назад", callback_data="admin_back")])
        kb = InlineKeyboardMarkup(inline_keyboard=kb_rows)
        await callback.message.edit_text("Выберите аккаунт:", reply_markup=kb)
    finally:
        session.close()
    await callback.answer()


@dp.callback_query(F.data.startswith("edit_select_"))
async def edit_select(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    acc_id = int(callback.data.split("_")[2])
    session = SessionLocal()
    try:
        acc = session.query(Account).filter_by(id=acc_id).first()
        if not acc:
            await callback.answer("Не найден")
            return
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✏️ Название", callback_data=f"edit_field_{acc_id}_name")],
            [InlineKeyboardButton(text="✏️ Описание", callback_data=f"edit_field_{acc_id}_description")],
            [InlineKeyboardButton(text="✏️ Цена", callback_data=f"edit_field_{acc_id}_price")],
            [InlineKeyboardButton(text="📁 Заменить tdata", callback_data=f"edit_field_{acc_id}_tdata_file_id")],
            [InlineKeyboardButton(text="✏️ Текст tdata", callback_data=f"edit_field_{acc_id}_tdata_text")],
            [InlineKeyboardButton(text="🗑 Удалить", callback_data=f"delete_acc_{acc_id}")],
            [InlineKeyboardButton(text="🔙 Назад", callback_data="edit_accounts")]
        ])
        await callback.message.edit_text(f"Редактирование: {acc.name}\nВыберите поле:", reply_markup=kb)
    finally:
        session.close()
    await callback.answer()


@dp.callback_query(F.data.startswith("edit_field_"))
async def edit_field(callback: types.CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return
    parts = callback.data.split("_")
    acc_id = int(parts[2])
    field = "_".join(parts[3:])
    await state.update_data(edit_acc_id=acc_id, edit_field=field)
    if field == "tdata_file_id":
        await callback.message.edit_text("Отправьте новый файл tdata документом:")
    elif field == "price":
        await callback.message.edit_text("Введите новую цену (число):")
    else:
        await callback.message.edit_text("Введите новое значение (или «-» чтобы очистить):")
    await state.set_state(EditAccountStates.value)
    await callback.answer()


@dp.message(EditAccountStates.value, F.document)
async def edit_value_doc(message: types.Message, state: FSMContext):
    data = await state.get_data()
    if data.get('edit_field') != 'tdata_file_id':
        await message.answer("Сейчас ожидается текст, а не файл.")
        return
    session = SessionLocal()
    try:
        acc = session.query(Account).filter_by(id=data['edit_acc_id']).first()
        if acc:
            acc.tdata_file_id = message.document.file_id
            session.commit()
            await message.answer("✅ tdata обновлена.", reply_markup=admin_main_keyboard())
    finally:
        session.close()
    await state.clear()


@dp.message(EditAccountStates.value, F.text)
async def edit_value_text(message: types.Message, state: FSMContext):
    data = await state.get_data()
    acc_id = data.get('edit_acc_id')
    field = data.get('edit_field')
    if not acc_id or not field:
        await message.answer("Ошибка сессии.")
        await state.clear()
        return
    new_value = message.text.strip()
    if new_value == '-':
        new_value = None
    session = SessionLocal()
    try:
        acc = session.query(Account).filter_by(id=acc_id).first()
        if not acc:
            await message.answer("Аккаунт не найден.")
            await state.clear()
            return
        if field == 'price':
            try:
                new_value = int(new_value)
                if new_value <= 0:
                    raise ValueError
            except (ValueError, TypeError):
                await message.answer("Цена должна быть положительным числом.")
                return
        if field == 'name' and new_value is None:
            await message.answer("Название не может быть пустым.")
            return
        setattr(acc, field, new_value)
        session.commit()
        await message.answer(f"✅ Поле «{field}» обновлено.", reply_markup=admin_main_keyboard())
    finally:
        session.close()
    await state.clear()


@dp.callback_query(F.data.startswith("delete_acc_"))
async def delete_account(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    acc_id = int(callback.data.split("_")[2])
    session = SessionLocal()
    try:
        acc = session.query(Account).filter_by(id=acc_id).first()
        if acc:
            session.delete(acc)
            session.commit()
            await callback.message.edit_text("🗑 Удалено.", reply_markup=admin_main_keyboard())
        else:
            await callback.message.edit_text("Не найдено.", reply_markup=admin_main_keyboard())
    finally:
        session.close()
    await callback.answer()


# ==========================================================
#  ЧАТ ПОДДЕРЖКИ
# ==========================================================
@dp.callback_query(F.data == "support")
async def support_user(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(SupportStates.waiting)
    await callback.message.edit_text(
        "💬 Напишите сообщение администратору. Он ответит вам в этом чате.\n"
        "Чтобы выйти — нажмите любую кнопку меню ниже.",
        reply_markup=user_main_keyboard()
    )
    await callback.answer()


@dp.message(SupportStates.waiting, F.text)
async def handle_user_support(message: types.Message, state: FSMContext):
    if message.text.startswith('/'):
        await state.clear()
        return
    session = SessionLocal()
    try:
        user = get_user(session, message.from_user.id)
        msg = SupportMessage(user_id=user.id, from_admin=False, message=message.text)
        session.add(msg)
        session.commit()
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💬 Ответить", callback_data=f"reply_to_{user.telegram_id}")]
        ])
        await bot.send_message(ADMIN_ID,
                               f"💬 Сообщение от {message.from_user.id}:\n\n{message.text}",
                               reply_markup=kb)
        await message.answer("✅ Отправлено в поддержку. Ожидайте ответа.")
    finally:
        session.close()


@dp.callback_query(F.data == "admin_support")
async def admin_support_list(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    if not is_admin(callback.from_user.id):
        return
    session = SessionLocal()
    try:
        users_with_msgs = session.query(SupportMessage.user_id).distinct().all()
        if not users_with_msgs:
            await callback.message.edit_text("Сообщений нет.", reply_markup=admin_main_keyboard())
            await callback.answer()
            return
        kb_rows = []
        for (uid,) in users_with_msgs:
            u = session.query(User).filter_by(id=uid).first()
            if u:
                kb_rows.append([
                    InlineKeyboardButton(text=f"👤 {u.telegram_id}",
                                         callback_data=f"support_chat_{u.telegram_id}")
                ])
        kb_rows.append([InlineKeyboardButton(text="🔙 Назад", callback_data="admin_back")])
        kb = InlineKeyboardMarkup(inline_keyboard=kb_rows)
        await callback.message.edit_text("Выберите пользователя:", reply_markup=kb)
    finally:
        session.close()
    await callback.answer()


@dp.callback_query(F.data.startswith("support_chat_"))
async def support_chat(callback: types.CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return
    user_telegram_id = int(callback.data.split("_")[2])
    session = SessionLocal()
    try:
        user = session.query(User).filter_by(telegram_id=user_telegram_id).first()
        if not user:
            await callback.answer("Не найден")
            return
        history = session.query(SupportMessage).filter_by(user_id=user.id)\
            .order_by(SupportMessage.created_at).limit(20).all()
        text = f"💬 Переписка с {user_telegram_id}:\n\n"
        for m in history:
            text += f"{'👤 Админ' if m.from_admin else '👤 Юзер'}: {m.message}\n"
        if not history:
            text += "История пуста."
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✏️ Написать", callback_data=f"admin_reply_{user_telegram_id}")],
            [InlineKeyboardButton(text="🔙 Назад", callback_data="admin_support")]
        ])
        await callback.message.edit_text(text, reply_markup=kb)
        await state.update_data(support_user_id=user.id, support_telegram_id=user_telegram_id)
    finally:
        session.close()
    await callback.answer()


@dp.callback_query(F.data.startswith("admin_reply_"))
async def admin_reply_start(callback: types.CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return
    await state.update_data(reply_to_telegram_id=int(callback.data.split("_")[2]))
    await callback.message.edit_text("Введите текст ответа:")
    await state.set_state(AdminSupportStates.chat)
    await callback.answer()


@dp.callback_query(F.data.startswith("reply_to_"))
async def reply_to_user(callback: types.CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return
    await state.update_data(reply_to_telegram_id=int(callback.data.split("_")[2]))
    await callback.message.answer("Введите текст ответа:")
    await state.set_state(AdminSupportStates.chat)
    await callback.answer()


@dp.message(AdminSupportStates.chat, F.text)
async def admin_reply_send(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    data = await state.get_data()
    user_telegram_id = data.get('reply_to_telegram_id')
    if not user_telegram_id:
        await message.answer("Ошибка: получатель не найден.")
        await state.clear()
        return
    session = SessionLocal()
    try:
        user = session.query(User).filter_by(telegram_id=user_telegram_id).first()
        if user:
            session.add(SupportMessage(user_id=user.id, from_admin=True, message=message.text))
            session.commit()
            await bot.send_message(user_telegram_id, f"💬 Ответ от администратора:\n\n{message.text}")
            await message.answer("✅ Отправлено.", reply_markup=admin_main_keyboard())
        else:
            await message.answer("Пользователь не найден.", reply_markup=admin_main_keyboard())
    finally:
        session.close()
    await state.clear()


# ==========================================================
#  ФОЛБЭК
# ==========================================================
@dp.message(F.text)
async def fallback_text(message: types.Message):
    if is_admin(message.from_user.id):
        return
    await message.answer("Выберите действие в меню или напишите в поддержку через «💬 Поддержка».",
                         reply_markup=user_main_keyboard())


@dp.callback_query()
async def unknown_callback(callback: types.CallbackQuery):
    await callback.answer("Неизвестная команда.")


# ==========================================================
#  ФОНОВЫЕ ЗАДАЧИ
# ==========================================================
async def clear_locks_loop():
    while True:
        await asyncio.sleep(60)
        session = SessionLocal()
        try:
            clear_expired_locks(session)
        except Exception as e:
            logging.error(f"clear_locks: {e}")
        finally:
            session.close()


async def crypto_poll_loop():
    while True:
        await asyncio.sleep(60)
        session = SessionLocal()
        try:
            active_invoices = session.query(CryptoInvoice).filter_by(status='active').all()
            for inv in active_invoices:
                try:
                    info = await crypto_get_invoice(inv.invoice_id)
                    if not info or info.get("status") != "paid":
                        continue

                    updated = session.query(CryptoInvoice).filter_by(
                        id=inv.id, status='active'
                    ).update({'status': 'paid'})
                    if not updated:
                        session.rollback()
                        continue

                    user = inv.user
                    user.balance += inv.amount
                    session.add(DepositRequest(
                        user_id=user.id, amount=inv.amount,
                        tg_id_entered=str(user.telegram_id),
                        method='crypto', status='confirmed'))
                    session.commit()

                    try:
                        await bot.send_message(user.telegram_id,
                                               f"✅ Крипто-оплата! Баланс пополнен на {inv.amount} руб.")
                        await bot.send_message(ADMIN_ID,
                                               f"🪙 Крипто-пополнение\n👤 {user.telegram_id}\n💰 {inv.amount} руб.")
                    except Exception:
                        pass
                except Exception as inner_e:
                    session.rollback()
                    logging.error(f"crypto_poll inner: {inner_e}")
        except Exception as e:
            logging.error(f"crypto_poll: {e}")
        finally:
            session.close()


# ==========================================================
#  ЗАПУСК
# ==========================================================
async def main():
    if not TELEGRAM_API_ID or not TELEGRAM_API_HASH:
        logging.warning("⚠️ TELEGRAM_API_ID / TELEGRAM_API_HASH не заданы!")
    if not CRYPTO_PAY_TOKEN or "ВАШ" in CRYPTO_PAY_TOKEN:
        logging.warning("⚠️ CRYPTO_PAY_TOKEN не задан!")
    asyncio.create_task(clear_locks_loop())
    asyncio.create_task(crypto_poll_loop())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
