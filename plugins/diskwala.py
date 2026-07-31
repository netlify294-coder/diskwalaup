from pyrogram import Client, filters, StopPropagation
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.tl.functions.messages import RequestAppWebViewRequest
from telethon.tl.types import InputBotAppShortName, InputPeerSelf, DataJSON
from urllib.parse import urlparse, unquote
import motor.motor_asyncio, asyncio, requests, time, re, os, shutil, uuid, json, logging
from config import *
from pyrogram.errors import UserIsBlocked, QueryIdInvalid, UserNotParticipant
import qrcode, io, pytz as pt
import secrets
import datetime as dl
from collections import defaultdict
from pyrogram.enums import ParseMode
from pyrogram.types import CallbackQuery
import aiohttp
from urllib.parse import quote
import requests
import time

user_orders: dict[str, dict] = {}
active_qr_sessions: dict[int, str] = {}
user_locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

# Ensures admin reposts (multiple posts sent back-to-back) are fully
# completed one at a time — download + upload + repost for post #1 finishes
# before post #2 starts, so videos never get interleaved/mixed up.
admin_repost_lock = asyncio.Lock()
_free_trial_used: set[int] = set()  # backed by DB below, this is just a fast-path cache

mongo = motor.motor_asyncio.AsyncIOMotorClient(DB_URI)[DB_NAME]
db, dumps_col, meta_col, cache_col = mongo.users, mongo.dumps, mongo.meta, mongo.file_cache
shortlinks_col = mongo.shortlinks
repost_channels_col = mongo.repost_channels
tg = TelegramClient(StringSession(SESSION), API_ID, API_HASH)

BOT_USERNAME, APP_SHORT_NAME = "sky577bot", "open"
API_URL = "https://api2.diskwala.net/api/diskwala/download"
RE = re.compile(r"https?://(?:www\.)?diskwala\.com/app/[A-Za-z0-9]+")
CMDS = ["start", "stats", "adddump", "deldump", "dumps", "addpaid", "delpremium", "premium","broadcast", "link", "panel", "checkchannels", "addpost", "delpost", "postchannels"]


def is_stale_message(m: Message, max_age_seconds: int = 120) -> bool:
    """After a restart/redeploy, Telegram delivers every message the bot
    missed while it was down, all at once. Without this guard, that whole
    backlog gets processed simultaneously — flooding @Diskwaladsbot with
    many links at once and breaking the single-request-at-a-time flow.
    Ignore anything older than max_age_seconds instead."""
    try:
        msg_time = m.date.timestamp() if hasattr(m.date, "timestamp") else m.date
        return (time.time() - msg_time) > max_age_seconds
    except Exception:
        return False

DOWNLOAD_DIR = "downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)
ARIA2C_PATH = shutil.which("aria2c") or "aria2c"

MAX_CONCURRENT_DOWNLOADS = 6
download_semaphore = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)

_PCT_RE = re.compile(r"\((\d{1,3})%\)")
_SPEED_RE = re.compile(r"DL:([\d.]+\w+)")

_link_locks: dict[str, asyncio.Lock] = {}
_locks_guard = asyncio.Lock()

# ── Free-tier / premium config ───────────────────────────────────
FREE_LIMIT = 10  # number of full downloads a non-premium user gets before stream-only mode


async def update_qr_timer(client, msg, order_id, orders, sessions, user_id, total_seconds=300):
    for remaining in range(total_seconds, 0, -5):
        await asyncio.sleep(5)
        if order_id not in orders or orders[order_id]["used"]:
            return
        mins, secs = divmod(remaining - 5, 60)
        try:
            await msg.edit_reply_markup(
                InlineKeyboardMarkup([[InlineKeyboardButton(
                    f"⏳ 𝖤𝖷𝖯𝖨𝖱𝖤𝖲 𝖨𝖭 {mins:02d}:{secs:02d}", callback_data="none")]])
            )
        except Exception:
            pass

# ── Premium purchase & payment verification ───────────────────────
@Client.on_callback_query(filters.regex("^(buy_premium|plan_|free_trial|retry_)"))
async def buy_and_verify_handler(client: Client, query: CallbackQuery):
    await query.answer()

    user_id = query.from_user.id
    data = query.data

    # -------- PLAN MENU --------
    if data == "buy_premium":
        rows, row = [], []
        for i, p in enumerate(PLANS):
            row.append(InlineKeyboardButton(f"{p['label']} · {p['price']}", callback_data=f"plan_{i}"))
            if len(row) == 2:
                rows.append(row); row = []
        if row:
            rows.append(row)


        return await query.message.edit_text(
            "💳 <b>𝗖𝗛𝗢𝗢𝗦𝗘 𝗔 𝗣𝗟𝗔𝗡</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "🏦 <b>𝖯𝖠𝖸𝖳𝖬 • 𝖴𝖯𝖨 • 𝖯𝖧𝖮𝖭𝖤𝖯𝖤 • 𝖦𝖯𝖠𝖸</b>\n\n"
            "✦ Pʀᴇᴍɪᴜᴍ ᴀᴄᴛɪᴠᴀᴛᴇs ᴀᴜᴛᴏᴍᴀᴛɪᴄᴀʟʟʏ ᴀғᴛᴇʀ ᴘᴀʏᴍᴇɴᴛ\n"
            "✦ Uɴʟɪᴍɪᴛᴇᴅ ғᴜʟʟ ᴅᴏᴡɴʟᴏᴀᴅs ✅\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "👇 Sᴇʟᴇᴄᴛ ʏᴏᴜʀ ᴘʟᴀɴ:</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(rows)
        )

    # -------- RETRY --------
    elif data.startswith("retry_"):
        order = user_orders.get(data.split("_", 1)[1])
        if not order or order["used"]:
            return await query.answer("⚠️ Invalid or already used order!", show_alert=True)
        for i, p in enumerate(PLANS):
            if p["days"] == order["days"]:
                data = f"plan_{i}"
                break

    # -------- GENERATE QR --------
    if data.startswith("plan_"):
        async with user_locks[user_id]:

            if user_id in active_qr_sessions:
                return await query.answer("⚠️ You have a pending payment. Complete it first!", show_alert=True)

            plan = PLANS[int(data.split("_")[1])]
            amount = int(plan["price"].replace("₹", ""))
            order_id = f"DSKWALA-{user_id}_{int(time.time())}"
            account_used = ACTIVE_PAYMENT

            active_qr_sessions[user_id] = order_id
            user_orders[order_id] = {
                "user": user_id, "amount": amount, "days": plan["days"],
                "used": False, "account": account_used,
            }

            upi = (f"upi://pay?pa={PAYMENT_ACCOUNTS[account_used]['upi']}&pn=Premium"
                   f"&am={amount}&cu=INR&tn={order_id}&tr={order_id}")
            
            wait_qrmsg = await query.message.reply_sticker("CAACAgEAAxkBAAEBXB9qWOBi8ijl1-QJYCBjIhOd1xsrFAACPAoAAsesyEaOswABHsPR5XQeBA")

            qr = qrcode.make(upi)
            bio = io.BytesIO(); bio.name = "qr.png"
            qr.save(bio); bio.seek(0)

            msg = await client.send_photo(
                query.message.chat.id, bio,
                caption=(
                    "<blockquote>✦ <b>𝖯𝗅𝖾𝖺𝗌𝖾 𝖼𝗈𝗆𝗉𝗅𝖾𝗍𝖾 𝗍𝗁𝖾 𝖿𝗈𝗅𝗅𝗈𝗐𝗂𝗇𝗀 𝗉𝖺𝗒𝗆𝖾𝗇𝗍:</b></blockquote>\n"
                    "━━━━━━━━━━━━━━━━━━━━\n\n"
                    f"❐ <b>𝖠𝖬𝖮𝖴𝖭𝖳 :</b>  {plan['price']}\n"
                    f"≡ <b>𝖯𝖫𝖠𝖭 :</b>  {plan['label']}\n"
                    f"❐ <b>𝖮𝖱𝖣𝖤𝖱 𝖨𝖣 :</b>  <code>{order_id}</code>\n\n"
                    "━━━━━━━━━━━━━━━━━━━━\n"
                    "<b>⏳𝖥𝗈𝗅𝗅𝗈𝗐 𝗍𝗁𝖾 𝖨𝗇𝗌𝗍𝗋𝗎𝖼𝗍𝗂𝗈𝗇:-</b>\n"
                    "1️⃣ 𝖲𝖼𝖺𝗇 𝗍𝗁𝖾 𝖰𝖱 𝖺𝗇𝖽 𝗉𝖺𝗒 𝗍𝗁𝖾 𝖺𝗆𝗈𝗎𝗇𝗍.\n"
                    "2️⃣ 𝖠𝖿𝗍𝖾𝗋 𝖯𝖺𝗒𝗆𝖾𝗇𝗍, 𝖢𝗅𝗂𝖼𝗄 𝗈𝗇 ✅ 𝖯𝖺𝗒𝗆𝖾𝗇𝗍 𝖣𝗈𝗇𝖾 𝖡𝗎𝗍𝗍𝗈𝗇.\n"
                    "3️⃣ 𝖡𝗈𝗍 𝗐𝗂𝗅𝗅 𝗏𝖾𝗋𝗂𝖿𝗒 𝗒𝗈𝗎𝗋 𝗉𝖺𝗒𝗆𝖾𝗇𝗍 𝖺𝗇𝖽 𝖺𝖼𝗍𝗂𝗏𝖺𝗍𝖾 𝗒𝗈𝗎𝗋 𝗉𝗅𝖺𝗇.\n\n"
                    "🗒𝖳𝗁𝗂𝗌 𝖰𝖱 𝖢𝗈𝖽𝖾 𝗐𝗂𝗅𝗅 𝖾𝗑𝗉𝗂𝗋𝖾 𝗂𝗇 5 𝖬𝗂𝗇𝗎𝗍𝖾𝗌. 𝖼𝗈𝗆𝗉𝗅𝖾𝗍𝖾 𝗍𝗁𝖾 𝗉𝖺𝗒𝗆𝖾𝗇𝗍 𝗂𝗇 5 𝗆𝗂𝗇𝗎𝗍𝖾𝗌\n\n"
                    "<blockquote>𝖨𝖿 𝗒𝗈𝗎 𝖺𝗅𝗋𝖾𝖺𝖽𝗒 𝗉𝖺𝗂𝖽 𝗍𝗁𝖾 𝖺𝗆𝗈𝗎𝗇𝗍 𝖺𝗇𝖽 𝗌𝗍𝗂𝗅𝗅 𝗌𝗁𝗈𝗐𝗂𝗇𝗀 𝖯𝖺𝗒𝗆𝖾𝗇𝗍 𝖭𝗈𝗍 𝖥𝗈𝗎𝗇𝖽 𝗍𝗁𝖾𝗇 𝖢𝗈𝗇𝗍𝖺𝖼𝗍 "
                    "<a href='https://t.me/DumpAdminBot?text=<b>Hey%20my%20order%20ID%20is%20{order_id}.%20\n\nI%20paid%20but%20my%20premium%20is%20not%20activated.</b>'>@DumpAdminBot</a></blockquote></b>"
                ),
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("⏳ 𝖤𝖷𝖯𝖨𝖱𝖤𝖲 𝖨𝖭 05:00", callback_data="none")
                ]]),
                parse_mode=ParseMode.HTML
            )
            await wait_qrmsg.delete()

            asyncio.create_task(
                update_qr_timer(client, msg, order_id, user_orders, active_qr_sessions, user_id, total_seconds=300)
            )

            async def check():
                merchant = PAYMENT_ACCOUNTS[user_orders[order_id]["account"]]["merchant"]

                async with aiohttp.ClientSession() as s:
                    for _ in range(60):
                        await asyncio.sleep(5)
                        try:
                            async with s.get(f"{PAYMENT_VERIFY_API}?mid={merchant}&oid={order_id}") as r:
                                d = await r.json()
                        except Exception:
                            continue

                        if d.get("STATUS") == "TXN_SUCCESS" and int(float(d.get("TXNAMOUNT", 0))) == amount:
                            if user_orders[order_id]["used"]:
                                return
                            user_orders[order_id]["used"] = True
                            active_qr_sessions.pop(user_id, None)

                            await add_premium(user_id, plan["days"])

                            try:
                                await msg.delete()
                            except Exception:
                                pass

                            readable = (
                                dl.datetime.now(pt.timezone("Asia/Kolkata")) +
                                dl.timedelta(days=plan["days"])
                            ).strftime("%d-%b-%Y %I:%M %p")

                            channel_id = LOG_CHANNELS.get(user_orders[order_id]["account"])
                            if channel_id:
                                user = query.from_user
                                mention = f"<a href='tg://user?id={user.id}'>{user.first_name}</a>"
                                try:
                                    await client.send_message(
                                        channel_id,
                                        f"💎 <b>𝗣𝗥𝗘𝗠𝗜𝗨𝗠 𝗔𝗖𝗧𝗜𝗩𝗔𝗧𝗘𝗗</b>\n"
                                        f"━━━━━━━━━━━━━━━━━━━━\n"
                                        f"👤 <b>Usᴇʀ :</b> {mention}\n"
                                        f"🆔 <b>ID :</b> <code>{user_id}</code>\n"
                                        f"📦 <b>Pʟᴀɴ :</b> {plan['label']} Pʀᴇᴍɪᴜᴍ\n"
                                        f"💰 <b>Aᴍᴏᴜɴᴛ :</b> ₹{amount}\n"
                                        f"🧾 <b>Oʀᴅᴇʀ :</b> <code>{order_id}</code>\n"
                                        f"⏳ <b>Eхᴘɪʀᴇs :</b> {readable}\n"
                                        f"━━━━━━━━━━━━━━━━━━━━",
                                        parse_mode=ParseMode.HTML
                                    )
                                except Exception:
                                    pass

                            return await client.send_message(
                                query.message.chat.id,
                                f"🎉 <b>𝗣𝗥𝗘𝗠𝗜𝗨𝗠 𝗔𝗖𝗧𝗜𝗩𝗔𝗧𝗘𝗗 !</b>\n\n"
                                f"📦 <b>Pʟᴀɴ :</b> {plan['label']}\n"
                                f"⏳ <b>Eхᴘɪʀᴇs :</b> {readable}\n\n"
                                f"🔥 Eɴᴊᴏʏ ʏᴏᴜʀ ᴘʀᴇᴍɪᴜᴍ ᴀᴄᴄᴇss!",
                                parse_mode=ParseMode.HTML
                            )

                active_qr_sessions.pop(user_id, None)
                try:
                    await msg.delete()
                except Exception:
                    pass

                await client.send_message(
                    query.message.chat.id,
                    f"⚠️ <b>𝗤𝗥 𝗘𝗫𝗣𝗜𝗥𝗘𝗗</b>\n\n"
                    f"Yᴏᴜʀ ᴘᴀʏᴍᴇɴᴛ ᴡɪɴᴅᴏᴡ ʜᴀs ᴄʟᴏsᴇᴅ.\n\n"
                    f"🧾 <b>Oʀᴅᴇʀ :</b> <code>{order_id}</code>\n"
                    f"↩️ ᴜsᴇ /verify <code>{order_id}</code> ɪғ ʏᴏᴜ ᴀʟʀᴇᴀᴅʏ ᴘᴀɪᴅ.",
                    parse_mode=ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("🔄 𝖳𝖱𝖸 𝖠𝖦𝖠𝖨𝖭", callback_data=f"retry_{order_id}")],
                        [InlineKeyboardButton("🆘 𝖲𝖴𝖯𝖯𝖮𝖱𝖳", url="https://t.me/DumpAdminBot")]
                    ])
                )

            asyncio.create_task(check())


# ── Force-sub ──────────────────────────────────────────────────────
async def is_subscribed(app: Client, user_id: int) -> bool:
    try:
        member = await app.get_chat_member(FORCE_SUB_CHANNEL, user_id)
        return member.status not in ("left", "kicked", "banned")
    except UserNotParticipant:
        return False
    except Exception as e:
        # Fail-closed: any other error (bot not admin in FORCE_SUB_CHANNEL,
        # wrong channel username, channel not found, etc.) now blocks access
        # instead of silently letting everyone through. Check the logs for
        # the exact reason if this starts firing.
        LOGGER(__name__).error(f"is_subscribed check failed for {user_id}: {e}")
        return False


async def create_short_code(links: list[str]) -> str:
    """Stores one or more links under a short random code and returns it.
    Base64-encoding the raw link(s) easily exceeds Telegram's 64-character
    limit on the start= deep-link parameter (silently dropped if over), so
    we store the links in Mongo and hand out a short code instead."""
    while True:
        code = secrets.token_urlsafe(6)[:8]  # ~8 chars, always well under 64
        if not await shortlinks_col.find_one({"_id": code}):
            break
    await shortlinks_col.update_one({"_id": code}, {"$set": {"links": links}}, upsert=True)
    return code


async def resolve_short_code(code: str) -> list[str] | None:
    doc = await shortlinks_col.find_one({"_id": code})
    return doc["links"] if doc else None


def join_prompt_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 𝖩𝗈𝗂𝗇 𝖢𝗁𝖺𝗇𝗇𝖾𝗅", url=f"https://t.me/{FORCE_SUB_CHANNEL}")],
        [InlineKeyboardButton("🔄 𝖳𝗋𝗒 𝖠𝗀𝖺𝗂𝗇", callback_data="check_sub")],
    ])


@Client.on_callback_query(filters.regex("^check_sub$"))
async def check_sub_cb(app: Client, cq):
    if await is_subscribed(app, cq.from_user.id):
        await cq.message.edit_text("✅ 𝖳𝗁𝖺𝗇𝗄𝗌 𝖿𝗈𝗋 𝗃𝗈𝗂𝗇𝗂𝗇𝗀! 𝖭𝗈𝗐 𝗌𝖾𝗇𝖽 𝗆𝖾 𝖺 𝖣𝗂𝗌𝗄𝗐𝖺𝗅𝖺 𝗅𝗂𝗇𝗄.")
    else:
        await cq.answer("❌ You haven't joined yet.", show_alert=True)


# ── Helpers ────────────────────────────────────────────────────────
def human_size(n: float) -> str:
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if n < 1024:
            return f"{n:.2f} {unit}"
        n /= 1024
    return f"{n:.2f} PB"


async def add_bandwidth(n: int):
    await meta_col.update_one({"_id": "bandwidth"}, {"$inc": {"bytes": n}}, upsert=True)


async def get_bandwidth() -> int:
    doc = await meta_col.find_one({"_id": "bandwidth"})
    return doc["bytes"] if doc else 0


async def get_dumps() -> list[int]:
    return [d["_id"] async for d in dumps_col.find({})]


def _parse_channel_ref(v: str):
    v = v.strip()
    try:
        return int(v)
    except ValueError:
        return v if v.startswith("@") else f"@{v}"


async def get_repost_channels() -> list:
    """All channels a repost gets sent to: any added via /addpost, plus the
    REPOST_CHANNEL env var (if set) as a default, deduplicated."""
    channels = [d["_id"] async for d in repost_channels_col.find({})]
    if REPOST_CHANNEL and REPOST_CHANNEL not in channels:
        channels.append(REPOST_CHANNEL)
    return channels


async def add_repost_channel(cid):
    await repost_channels_col.update_one({"_id": cid}, {"$set": {"_id": cid}}, upsert=True)


async def del_repost_channel(cid):
    await repost_channels_col.delete_one({"_id": cid})


# ── Admin panel settings (checkout button + auto-delete timer) ────
_DEFAULT_SETTINGS = {
    "button_text": "CHECKOUT ♡",
    "button_url": "",
    "auto_delete_seconds": 0,
    "caption_template": "<blockquote>“<b>Below is Your Link</b> ↩️\n\n<b>{link}</b>”</blockquote>",
}


async def get_panel_settings() -> dict:
    doc = await meta_col.find_one({"_id": "panel_settings"})
    settings = dict(_DEFAULT_SETTINGS)
    if doc:
        settings.update({k: v for k, v in doc.items() if k != "_id"})
    return settings


async def set_panel_setting(key: str, value):
    await meta_col.update_one({"_id": "panel_settings"}, {"$set": {key: value}}, upsert=True)


# Tracks which admin setting an owner is currently mid-way through typing,
# e.g. pending_admin_input[owner_id] = "button_text"
pending_admin_input: dict[int, str] = {}


async def schedule_auto_delete(app: Client, chat_id: int, message_id: int, seconds: int):
    if seconds and seconds > 0:
        async def _delete_later():
            await asyncio.sleep(seconds)
            try:
                await app.delete_messages(chat_id, message_id)
            except Exception:
                pass
        asyncio.create_task(_delete_later())


async def add_dump(cid: int):
    await dumps_col.update_one({"_id": cid}, {"$set": {"_id": cid}}, upsert=True)


async def del_dump(cid: int):
    await dumps_col.delete_one({"_id": cid})


async def copy_to_dumps(app: Client, sent: Message, dump_ids: list[int]):
    async def _copy(cid):
        try:
            await app.copy_message(cid, sent.chat.id, sent.id)
        except Exception:
            pass
    if dump_ids:
        await asyncio.gather(*[_copy(c) for c in dump_ids])


async def get_cache(link: str):
    return await cache_col.find_one({"_id": link})


async def save_cache(link: str, chat_id: int, msg_id: int, name: str, size: int):
    await cache_col.update_one(
        {"_id": link},
        {"$set": {"chat_id": chat_id, "message_id": msg_id, "file_name": name,
                   "size": size, "cached_at": time.time()}},
        upsert=True,
    )


async def get_link_lock(link: str) -> asyncio.Lock:
    async with _locks_guard:
        return _link_locks.setdefault(link, asyncio.Lock())


async def try_deliver_from_cache(app: Client, m: Message, msg: Message, link: str, tag: str) -> bool:
    cached = await get_cache(link)
    if not cached:
        return False
    try:
        await msg.edit_text(f"<b>⚡ 𝖢𝖠𝖢𝖧𝖤𝖣 — 𝖨𝖭𝖲𝖳𝖠𝖭𝖳 𝖣𝖤𝖫𝖨𝖵𝖤𝖱𝖸 {tag}</b>\n\n<code>{cached['file_name']}</code>")
        sent = await app.copy_message(m.chat.id, cached["chat_id"], cached["message_id"])
        panel = await get_panel_settings()
        await schedule_auto_delete(app, sent.chat.id, sent.id, panel["auto_delete_seconds"])
        await msg.delete()
        return True
    except Exception:
        return False


async def add_premium(user_id: int, days: float):
    """Extends premium by `days` from now OR from current expiry, whichever is later."""
    doc = await db.find_one({"_id": user_id})
    now = dl.datetime.now(pt.utc)

    current_expiry = doc.get("premium_until") if doc else None
    if current_expiry is not None and current_expiry.tzinfo is None:
        # MongoDB returns naive datetimes even if stored as UTC-aware — reattach UTC
        current_expiry = pt.utc.localize(current_expiry)

    base = current_expiry if (current_expiry and current_expiry > now) else now
    new_expiry = base + dl.timedelta(days=days)

    await db.update_one(
        {"_id": user_id},
        {"$set": {"premium_until": new_expiry}},
        upsert=True,
    )


async def is_premium(user_id: int) -> bool:
    doc = await db.find_one({"_id": user_id})
    if not doc or not doc.get("premium_until"):
        return False
    expiry = doc["premium_until"]
    if expiry.tzinfo is None:
        expiry = pt.utc.localize(expiry)
    return expiry > dl.datetime.now(pt.utc)

async def get_premium_expiry(user_id: int):
    doc = await db.find_one({"_id": user_id})
    expiry = doc.get("premium_until") if doc else None
    if expiry is not None and expiry.tzinfo is None:
        expiry = pt.utc.localize(expiry)
    return expiry


async def set_premium(user_id: int, value: bool):
    await db.update_one({"_id": user_id}, {"$set": {"premium": value}}, upsert=True)


def _today_str() -> str:
    return dl.datetime.now(pt.utc).strftime("%Y-%m-%d")


async def get_free_used(user_id: int) -> int:
    doc = await db.find_one({"_id": user_id})
    if not doc:
        return 0
    if doc.get("free_used_date") != _today_str():
        return 0  # new day — previous count no longer applies
    return doc.get("free_used", 0)


async def increment_free_used(user_id: int):
    today = _today_str()
    doc = await db.find_one({"_id": user_id})
    if doc and doc.get("free_used_date") == today:
        await db.update_one({"_id": user_id}, {"$inc": {"free_used": 1}})
    else:
        await db.update_one(
            {"_id": user_id}, {"$set": {"free_used": 1, "free_used_date": today}}, upsert=True
        )


async def can_full_download(user_id: int) -> bool:
    if await is_premium(user_id):
        return True
    return await get_free_used(user_id) < FREE_LIMIT


def premium_prompt_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💎 𝖦𝖾𝗍 𝖯𝗋𝖾𝗆𝗂𝗎𝗆", callback_data="buy_premium")],
    ])


# ─────────────────────────── VIDEO METADATA ───────────────────────────
async def get_video_metadata(path: str):
    try:
        p = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "format=duration:stream=width,height",
            "-of", "json", path,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, _ = await p.communicate()
        data = json.loads(stdout.decode().strip())
        duration = float(data.get("format", {}).get("duration", 0))
        s = data.get("streams", [{}])[0]
        return duration, int(s.get("width", 0)), int(s.get("height", 0))
    except Exception as e:
        logging.warning(f"ffprobe metadata failed: {e}")
        return 0, 0, 0


async def get_video_info(path: str) -> dict:
    dur, w, h = await get_video_metadata(path)
    return {"duration": int(dur), "width": w or 1280, "height": h or 720}


async def generate_thumbnail(video_path: str, output_path: str, time_position: int = 10) -> str | None:
    try:
        duration, w, h = await get_video_metadata(video_path)
        w, h = w or 1280, h or 720

        if duration and duration > 1:
            time_position = min(time_position, max(1, int(duration * 0.5)))
        else:
            time_position = 0

        vf = f"scale=trunc(iw/2)*2:trunc(ih/2)*2,format=yuv420p,scale={min(w, 720)}:-2"

        p = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "panic",
            "-ss", str(time_position), "-i", video_path,
            "-vframes", "1", "-vf", vf, "-q:v", "1", "-pix_fmt", "yuv420p", output_path,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
        )
        try:
            await asyncio.wait_for(p.communicate(), timeout=15)
        except asyncio.TimeoutError:
            p.kill()
            return None

        return output_path if p.returncode == 0 and os.path.exists(output_path) and os.path.getsize(output_path) > 1024 else None
    except Exception as e:
        logging.warning(f"Thumbnail failed: {e}")
        return None


# ── Telethon token + API fetch ──────────────────────────────────────
async def get_init():
    if not tg.is_connected():
        await tg.connect()
    bot = await tg.get_input_entity(BOT_USERNAME)
    r = await tg(RequestAppWebViewRequest(
        peer=InputPeerSelf(),
        app=InputBotAppShortName(bot_id=bot, short_name=APP_SHORT_NAME),
        platform="android", write_allowed=True, start_param="", theme_params=DataJSON("{}"),
    ))
    return unquote(urlparse(r.url).fragment.split("tgWebAppData=", 1)[1].split("&tgWebAppVersion=", 1)[0])

# ── Diskwaladsbot proxy ─────────────────────────────────────────────
# Diskwala's own API now encrypts its responses, so instead of calling it
# directly we automate @Diskwaladsbot (a public bot that already handles
# the fetch/decrypt) via our own userbot account (`tg`, same Telethon
# session used above). We send it the link, then wait for the video to show
# up in VIDEO_STORAGE_CHANNEL — which is where Diskwaladsbot is configured
# (in its own settings, outside our code) to upload every video it fetches.
# Requests are sent ONE AT A TIME (global lock) so whatever video shows up
# next in that channel is unambiguously the answer — no reply-matching,
# and once it lands there our bot can copy it directly to users without
# ever downloading/re-uploading the file itself.
DISKWALADSBOT = "Diskwaladsbot"
_dab_logger = logging.getLogger("diskwaladsbot")
diskwaladsbot_lock = asyncio.Lock()
_pending_storage_video: asyncio.Future | None = None


if VIDEO_STORAGE_CHANNEL:
    @Client.on_message(filters.chat(VIDEO_STORAGE_CHANNEL) & (filters.video | filters.document))
    async def _on_new_storage_video(_, m: Message):
        global _pending_storage_video
        _dab_logger.info(f"new video in VIDEO_STORAGE_CHANNEL: msg_id={m.id}")
        if _pending_storage_video and not _pending_storage_video.done():
            _dab_logger.info("resolving current pending request with this video")
            _pending_storage_video.set_result(m)
        else:
            _dab_logger.info("nothing currently waiting for a video — ignoring")


async def fetch_via_diskwaladsbot(link: str, timeout: int = 150) -> Message:
    """Sends `link` to @Diskwaladsbot (via our userbot account) and waits
    for the resulting video to appear in VIDEO_STORAGE_CHANNEL. Only one
    request is ever in flight at a time (across the whole bot), so whatever
    video shows up next in that channel is unambiguously the answer.
    Returns the Pyrogram Message already sitting in VIDEO_STORAGE_CHANNEL —
    callers can copy_message it directly, no download needed.
    Raises on timeout or if VIDEO_STORAGE_CHANNEL isn't configured."""
    global _pending_storage_video

    if not VIDEO_STORAGE_CHANNEL:
        raise Exception("VIDEO_STORAGE_CHANNEL is not set — required for the Diskwaladsbot proxy")

    async with diskwaladsbot_lock:
        if not tg.is_connected():
            _dab_logger.info("tg client not connected — connecting now")
            await tg.connect()

        loop = asyncio.get_event_loop()
        fut = loop.create_future()
        _pending_storage_video = fut

        await tg.send_message(DISKWALADSBOT, link)
        _dab_logger.info(f"sent link to @{DISKWALADSBOT}: link={link}")
        try:
            result = await asyncio.wait_for(fut, timeout=timeout)
            _dab_logger.info("got video in storage channel")
            return result
        except asyncio.TimeoutError:
            _dab_logger.error(f"TIMEOUT waiting for video in VIDEO_STORAGE_CHANNEL")
            raise Exception(
                f"No video appeared in VIDEO_STORAGE_CHANNEL within {timeout}s — "
                f"check @{DISKWALADSBOT} is configured to upload there"
            )
        finally:
            _pending_storage_video = None

from urllib.parse import quote
import requests
import time

def fetch(link, auth):
    headers = {
        "Authorization": f"Bearer {auth}",
        "X-Bot-Id": "diskwala",
        "Origin": "https://miniapp.diskwala.net",
        "Referer": "https://miniapp.diskwala.net/",
        "Content-Type": "application/json",
        "Accept": "*/*",
        "User-Agent": "Mozilla/5.0",
    }

    # Detect service
    if "flezen.com" in link.lower():
        download_api = "https://api2.diskwala.net/api/flezen/download"
        status_api = "https://api2.diskwala.net/api/flezen/status?link="
    else:
        download_api = "https://api2.diskwala.net/api/diskwala/download"
        status_api = "https://api2.diskwala.net/api/diskwala/status?link="

    # Start job
    r = requests.post(
        download_api,
        headers=headers,
        json={"link": link},
        timeout=60,
    )

    data = r.json()

    if not data.get("ok"):
        raise Exception(data.get("error", "Unknown API Error"))

    # Poll status
    status_url = status_api + quote(link, safe="")

    while True:
        r = requests.get(status_url, headers=headers, timeout=60)
        data = r.json()

        if not data.get("ok"):
            raise Exception(data.get("error", "Unknown API Error"))

        status = data.get("status", "").lower()

        if status == "pending":
            time.sleep(2)
            continue

        if status == "done":
            file = data.get("file")
            if not file:
                raise Exception(f"No file returned\n{data}")

            def _pick(d, *keys, required=True, default=None):
                for k in keys:
                    if k in d and d[k] not in (None, ""):
                        return d[k]
                if required:
                    raise Exception(
                        f"Diskwala/Flezen API response format changed — none of "
                        f"{keys} found. Raw 'file' object:\n{json.dumps(d, indent=2)[:1500]}"
                    )
                return default

            # Normalize Flezen response to Diskwala format
            if "flezen.com" in link.lower():
                return {
                    "name": _pick(file, "name", "fileName", "filename", "title"),
                    "size": _pick(file, "size", "fileSize", "length"),
                    "downloadUrl": _pick(file, "url", "downloadUrl", "download_url", "link"),
                    "thumb": None,
                    "type": file.get("type"),
                }

            # Diskwala response
            return {
                "name": _pick(file, "name", "fileName", "filename", "title"),
                "size": _pick(file, "size", "fileSize", "length"),
                "downloadUrl": _pick(file, "downloadUrl", "download_url", "url", "link"),
                "thumb": file.get("thumb"),
                "extension": file.get("extension"),
            }

        raise Exception(data)


async def run_aria2c(url: str, out_path: str, status_msg: Message, tag: str):
    if not (shutil.which(ARIA2C_PATH) or os.path.isfile(ARIA2C_PATH)):
        raise RuntimeError("aria2c not found. Install it (e.g. `apt install aria2`).")

    out_dir, out_name = os.path.dirname(out_path) or ".", os.path.basename(out_path)
    cmd = [ARIA2C_PATH, url, "-d", out_dir, "-o", out_name, "-x", "16", "-s", "16",
           "-k", "1M", "--summary-interval=2", "--console-log-level=warn", "--allow-overwrite=true"]

    process = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE,
                                                     stderr=asyncio.subprocess.STDOUT)
    last_edit = 0
    async for raw in process.stdout:
        line = raw.decode(errors="ignore").strip()
        pct_m, spd_m = _PCT_RE.search(line), _SPEED_RE.search(line)
        if pct_m and time.time() - last_edit > 3:
            speed = spd_m.group(1) if spd_m else "—"
            try:
                await status_msg.edit_text(
                    f"<b>⬇️ 𝖣𝖮𝖶𝖭𝖫𝖮𝖠𝖣𝖨𝖭𝖦... {tag}</b>",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(
                        f"⏳ {pct_m.group(1)}% • {speed}/s", callback_data="none")]])
                )
            except Exception:
                pass
            last_edit = time.time()

    await process.wait()
    if process.returncode != 0:
        raise RuntimeError(f"aria2c exited with code {process.returncode}")


# ── Commands ──────────────────────────────────────────────────────
@Client.on_message(filters.command("start") & filters.private)
async def start(app: Client, m: Message):
    await db.update_one({"_id": m.from_user.id}, {"$set": {"name": m.from_user.first_name}}, upsert=True)

    # Deep-link support: https://t.me/<bot>?start=<short_code>
    # The code is generated by /link or the admin repost feature and looked
    # up in Mongo — this avoids Telegram's 64-char limit on start= that a
    # raw/encoded Diskwala URL would blow past. We still also check the raw
    # text in case someone pastes a link directly after /start.
    payload = m.text.split(None, 1)[1] if len(m.command) > 1 else ""
    links = RE.findall(payload)
    if not links and payload:
        resolved = await resolve_short_code(payload)
        if resolved:
            links = resolved

    if links:
        if not await is_subscribed(app, m.from_user.id):
            await m.reply(
                f"<b>🔒 𝖩𝗈𝗂𝗇 𝗈𝗎𝗋 𝖼𝗁𝖺𝗇𝗇𝖾𝗅 𝗍𝗈 𝗎𝗌𝖾 𝗍𝗁𝗂𝗌 𝖻𝗈𝗍</b>\n\n"
                f"Join @{FORCE_SUB_CHANNEL}, then send your link again.",
                reply_markup=join_prompt_markup(),
            )
            return

        premium = await is_premium(m.from_user.id)
        used = await get_free_used(m.from_user.id)
        total = len(links)
        tasks = []

        if premium or used < FREE_LIMIT:
            for i, link in enumerate(links):
                tasks.append(process_link(app, m, link, i + 1, total))
            await asyncio.gather(*tasks, return_exceptions=True)
            if not premium:
                await increment_free_used(m.from_user.id)
        else:
            for i, link in enumerate(links):
                tag = f"[{i + 1}/{total}]"
                msg = await m.reply(
                    f"<blockquote><b>⚡ 𝖥𝖤𝖳𝖢𝖧𝖨𝖭𝖦 𝖫𝖨𝖭𝖪 {tag}...</b></blockquote>"
                )
                tasks.append(deliver_stream_only(m, msg, link, tag))
            await asyncio.gather(*tasks, return_exceptions=True)
        return

    try:
        await m.reply("<b>👋 Send me a Diskwala or Flezen link.</b>")
    except UserIsBlocked:
        pass


@Client.on_message(filters.command("link") & filters.private)
async def make_deep_link(app: Client, m: Message):
    """/link <diskwala_url> — generates a shareable deep link that starts
    the bot and immediately delivers that link's video."""
    if len(m.command) < 2:
        return await m.reply(
            "<b>Usage:</b> <code>/link https://www.diskwala.com/app/xxxxx</code>"
        )

    target = m.command[1]
    if not RE.match(target):
        return await m.reply("⚠️ That doesn't look like a valid Diskwala/Flezen link.")

    me = await app.get_me()
    code = await create_short_code([target])
    deep_link = f"https://t.me/{me.username}?start={code}"

    await m.reply(
        f"<b>🔗 Your deep link:</b>\n<code>{deep_link}</code>\n\n"
        "Anyone who opens this will start the bot and get this video automatically."
    )


@Client.on_message(filters.command("stats") & filters.user(OWNER_ID))
async def stats(_, m):
    users, dumps, bw = await db.count_documents({}), await get_dumps(), await get_bandwidth()
    premium_count = await db.count_documents({"premium": True})
    await m.reply(
        f"👥 Users: {users}\n"
        f"💎 Premium: {premium_count}\n"
        f"📡 Dump channels: {len(dumps)}\n"
        f"📊 Bandwidth used: <code>{human_size(bw)}</code>"
    )

PREMIUM_STICKER = "CAACAgEAAxkBAAEBXB5qWOBdtSOL6gI_ul76mhFn7JdFdQACOAwAAu3CwUb78hzfVdJX-R4E"


import re as _re

_DURATION_RE = _re.compile(r"^(\d+(?:\.\d+)?)([mhdwMy])$")

_DURATION_UNITS = {
    "m": 1 / 1440,        # minutes → fraction of a day
    "h": 1 / 24,          # hours → fraction of a day
    "d": 1,                # days
    "w": 7,                # weeks
    "M": 30,                # months (approx)
    "y": 365,               # years (approx)
}


def parse_duration(text: str) -> float | None:
    """Parses '7d', '1m', '2h', '3w', '1M', '1y' into a number of days. Returns None if invalid."""
    match = _DURATION_RE.match(text.strip())
    if not match:
        return None
    value, unit = match.groups()
    days = float(value) * _DURATION_UNITS[unit]
    if not (0 < days <= 3650):  # sanity cap ~10 years
        return None
    return days


@Client.on_message(filters.command("addpaid") & filters.user(OWNER_ID))
async def addpaid(client, m):
    """Usage: /addpaid <user_id> <duration>
    Duration examples: 30m (30 min), 2h (2 hours), 7d (7 days), 2w (2 weeks), 1M (1 month), 1y (1 year)
    """
    parts = m.text.split()[1:]
    if len(parts) != 2:
        return await m.reply(
            "⚠️ Usage: <code>/addpaid user_id duration</code>\n"
            "Examples:\n"
            "<code>/addpaid 123456789 7d</code> — 7 days\n"
            "<code>/addpaid 123456789 1m</code> — 1 minute\n"
            "<code>/addpaid 123456789 1M</code> — 1 month\n"
            "<code>/addpaid 123456789 2h</code> — 2 hours\n"
            "<code>/addpaid 123456789 1y</code> — 1 year"
        )

    uid_str, dur_str = parts

    try:
        uid = int(uid_str)
    except ValueError:
        return await m.reply(f"⚠️ Invalid user ID: <code>{uid_str}</code>")

    days = parse_duration(dur_str)
    if days is None:
        return await m.reply(
            f"⚠️ Invalid duration: <code>{dur_str}</code>\n"
            "Use a number + unit: <code>m</code>=minute, <code>h</code>=hour, <code>d</code>=day, "
            "<code>w</code>=week, <code>M</code>=month, <code>y</code>=year\n"
            "Example: <code>7d</code>, <code>1M</code>, <code>2h</code>"
        )

    await add_premium(uid, days)

    expiry = await get_premium_expiry(uid)
    readable = expiry.astimezone(pt.timezone("Asia/Kolkata")).strftime("%d-%b-%Y %I:%M %p")

    # Notify the user directly — best-effort
    notified = True
    try:
        try:
            await client.send_sticker(uid, PREMIUM_STICKER)
        except Exception:
            pass

        await client.send_message(
            uid,
            f"🎉 <b>𝗣𝗥𝗘𝗠𝗜𝗨𝗠 𝗔𝗖𝗧𝗜𝗩𝗔𝗧𝗘𝗗 !</b>\n\n"
            f"📦 <b>Gʀᴀɴᴛᴇᴅ :</b> {dur_str}\n"
            f"⏳ <b>Eхᴘɪʀᴇs :</b> {readable}\n\n"
            f"🔥 Eɴᴊᴏʏ ʏᴏᴜʀ ᴘʀᴇᴍɪᴜᴍ ᴀᴄᴄᴇss!",
            parse_mode=ParseMode.HTML,
        )
    except Exception:
        notified = False

    text = f"💎 Premium granted to <code>{uid}</code> for <b>{dur_str}</b>\n⏳ Expires: <code>{readable}</code>"
    if not notified:
        text += "\n🔕 Couldn't DM the user (blocked bot / never started chat)."
    await m.reply(text)


@Client.on_message(filters.command("delpremium") & filters.user(OWNER_ID))
async def delpremium(_, m):
    """Usage: /delpremium <user_id> [user_id...]"""
    parts = m.text.split()[1:]
    if not parts:
        return await m.reply("⚠️ Usage: <code>/delpremium user_id1 user_id2 ...</code>")

    done, failed = [], []
    for p in parts:
        try:
            uid = int(p)
        except ValueError:
            failed.append(p)
            continue
        await db.update_one({"_id": uid}, {"$unset": {"premium_until": ""}})
        done.append(str(uid))

    text = ""
    if done:
        text += f"🗑️ Premium revoked: <code>{', '.join(done)}</code>\n"
    if failed:
        text += f"❌ Invalid IDs: <code>{', '.join(failed)}</code>"
    await m.reply(text or "Nothing to remove.")

def premium_prompt_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💎 𝖡𝗎𝗒 𝖯𝗋𝖾𝗆𝗂𝗎𝗆", callback_data="buy_premium")],
    ])


@Client.on_message(filters.command("premium") & filters.private)
async def premium_status(_, m):
    uid = m.from_user.id
    if await is_premium(uid):
        expiry = await get_premium_expiry(uid)
        readable = expiry.astimezone(pt.timezone("Asia/Kolkata")).strftime("%d-%b-%Y %I:%M %p")
        await m.reply(f"💎 You're <b>Premium</b> until <code>{readable}</code>.")
    else:
        used = await get_free_used(uid)
        left = max(0, FREE_LIMIT - used)
        await m.reply(
            f"🆓 Free downloads left: <b>{left}/{FREE_LIMIT}</b>\n"
            f"After that, links stream only (in-app), not downloadable.\n\n"
            f"Upgrade to Premium for unlimited full downloads.",
            reply_markup=premium_prompt_markup(),
        )


@Client.on_message(filters.command("adddump") & filters.user(OWNER_ID))
async def adddump(_, m):
    parts = m.text.split()[1:]
    if not parts:
        return await m.reply("⚠️ Usage: <code>/adddump -100xxxx -100yyyy ...</code>")
    added, failed = [], []
    for p in parts:
        try:
            cid = int(p)
        except ValueError:
            failed.append(p); continue
        await add_dump(cid); added.append(str(cid))
    text = (f"✅ Added: <code>{', '.join(added)}</code>\n" if added else "") + \
           (f"❌ Invalid: <code>{', '.join(failed)}</code>" if failed else "")
    await m.reply(text or "Nothing to add.")


@Client.on_message(filters.command("deldump") & filters.user(OWNER_ID))
async def deldump(_, m):
    parts = m.text.split()[1:]
    if not parts:
        return await m.reply("⚠️ Usage: <code>/deldump -100xxxx</code>")
    removed = []
    for p in parts:
        try:
            cid = int(p)
        except ValueError:
            continue
        await del_dump(cid); removed.append(str(cid))
    await m.reply(f"🗑️ Removed: <code>{', '.join(removed)}</code>" if removed else "Nothing removed.")


@Client.on_message(filters.command("checkchannels") & filters.user(OWNER_ID))
async def check_channels(app: Client, m: Message):
    """Diagnoses 'Peer id invalid' issues by trying to resolve each
    configured channel directly and reporting the exact result/error."""
    lines = []
    for label, cid in [("VIDEO_STORAGE_CHANNEL", VIDEO_STORAGE_CHANNEL), ("REPOST_CHANNEL", REPOST_CHANNEL)]:
        if not cid:
            lines.append(f"⚠️ {label}: not set in env vars")
            continue
        try:
            chat = await app.get_chat(cid)
            lines.append(f"✅ {label} (<code>{cid}</code>): resolved — {chat.title}")
        except Exception as e:
            lines.append(f"❌ {label} (<code>{cid}</code>): {type(e).__name__}: {e}")

    await m.reply("<b>🔍 Channel check:</b>\n\n" + "\n".join(lines))


@Client.on_message(filters.command("dumps") & filters.user(OWNER_ID))
async def list_dumps(_, m):
    dumps = await get_dumps()
    await m.reply("<b>📡 Dump channels:</b>\n" + "\n".join(f"• <code>{d}</code>" for d in dumps)
                  if dumps else "No dump channels configured.")


@Client.on_message(filters.command("addpost") & filters.user(OWNER_ID))
async def addpost(_, m):
    parts = m.text.split()[1:]
    if not parts:
        return await m.reply("⚠️ Usage: <code>/addpost -100xxxx channelusername ...</code>")
    added = []
    for p in parts:
        cid = _parse_channel_ref(p)
        await add_repost_channel(cid)
        added.append(str(cid))
    await m.reply(f"✅ Added post channel(s): <code>{', '.join(added)}</code>")


@Client.on_message(filters.command("delpost") & filters.user(OWNER_ID))
async def delpost(_, m):
    parts = m.text.split()[1:]
    if not parts:
        return await m.reply("⚠️ Usage: <code>/delpost -100xxxx</code>")
    removed = []
    for p in parts:
        cid = _parse_channel_ref(p)
        await del_repost_channel(cid)
        removed.append(str(cid))
    await m.reply(f"🗑️ Removed post channel(s): <code>{', '.join(removed)}</code>")


@Client.on_message(filters.command("postchannels") & filters.user(OWNER_ID))
async def list_post_channels(_, m):
    channels = await get_repost_channels()
    await m.reply(
        "<b>📮 Repost (post) channels:</b>\n" + "\n".join(f"• <code>{c}</code>" for c in channels)
        if channels else "No post channels configured."
    )


def _panel_markup(s: dict) -> InlineKeyboardMarkup:
    delete_label = "Off" if not s["auto_delete_seconds"] else f"{s['auto_delete_seconds']}s"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📝 Repost Caption Template", callback_data="panel_set_caption_template")],
        [InlineKeyboardButton(f"✏️ Button Text: {s['button_text']}", callback_data="panel_set_button_text")],
        [InlineKeyboardButton(f"🔗 Button URL: {s['button_url'] or 'not set'}", callback_data="panel_set_button_url")],
        [InlineKeyboardButton(f"⏱ Auto-Delete (user videos): {delete_label}", callback_data="panel_set_auto_delete")],
        [InlineKeyboardButton("🔄 Refresh", callback_data="panel_refresh")],
    ])


@Client.on_message(filters.command("panel") & filters.user(OWNER_ID))
async def admin_panel(_, m: Message):
    s = await get_panel_settings()
    await m.reply(
        "<b>⚙️ Admin Panel</b>\n\n"
        "Tap a setting below to change it. These apply to every repost "
        "(checkout button) and every video delivery (auto-delete).",
        reply_markup=_panel_markup(s),
    )


@Client.on_callback_query(filters.regex("^panel_refresh$") & filters.user(OWNER_ID))
async def panel_refresh_cb(_, cq: CallbackQuery):
    s = await get_panel_settings()
    await cq.message.edit_reply_markup(_panel_markup(s))
    await cq.answer("Refreshed")


@Client.on_callback_query(filters.regex("^panel_set_(button_text|button_url|auto_delete|caption_template)$") & filters.user(OWNER_ID))
async def panel_set_cb(_, cq: CallbackQuery):
    key = cq.data.replace("panel_set_", "")
    pending_admin_input[cq.from_user.id] = key
    prompts = {
        "button_text": "Send the new <b>button text</b> (e.g. <code>CHECKOUT ♡</code>):",
        "button_url": "Send the new <b>button URL</b> (must start with http:// or https://):",
        "auto_delete": "Send the <b>auto-delete time in seconds</b> for videos delivered to users (send <code>0</code> to turn it off):",
        "caption_template": "Send the new <b>repost caption template</b>. Must include <code>{link}</code> "
                             "where the combined link should go. HTML tags like &lt;b&gt;, &lt;blockquote&gt; work.\n\n"
                             "Current:\n<code>" + (await get_panel_settings())["caption_template"].replace("<", "&lt;").replace(">", "&gt;") + "</code>",
    }
    await cq.answer()
    await cq.message.reply(prompts[key])


@Client.on_message(filters.private & filters.user(OWNER_ID) & filters.text & ~filters.command(CMDS))
async def admin_panel_input(_, m: Message):
    uid = m.from_user.id
    if uid not in pending_admin_input:
        return  # not answering a panel prompt — let normal handlers process this

    key = pending_admin_input.pop(uid)
    value = m.text.strip()

    if key == "button_text":
        await set_panel_setting("button_text", value)
        await m.reply(f"✅ Button text set to: <code>{value}</code>")

    elif key == "button_url":
        if not value.startswith("http://") and not value.startswith("https://"):
            pending_admin_input[uid] = key  # keep waiting, didn't consume this attempt
            return await m.reply("⚠️ URL must start with http:// or https://. Try again:")
        await set_panel_setting("button_url", value)
        await m.reply(f"✅ Button URL set to: <code>{value}</code>")

    elif key == "auto_delete":
        if not value.isdigit():
            pending_admin_input[uid] = key
            return await m.reply("⚠️ Send a plain number of seconds (0 = off). Try again:")
        await set_panel_setting("auto_delete_seconds", int(value))
        label = "disabled" if value == "0" else f"{value} seconds"
        await m.reply(f"✅ Auto-delete set to: {label}")

    elif key == "caption_template":
        if "{link}" not in value:
            pending_admin_input[uid] = key
            return await m.reply("⚠️ Template must include {link} somewhere. Try again:")
        await set_panel_setting("caption_template", value)
        await m.reply("✅ Caption template updated.")

    raise StopPropagation


@Client.on_callback_query(filters.regex("^none$"))
async def ignore_cb(_, cq):
    try:
        await cq.answer()
    except QueryIdInvalid:
        pass
    except Exception:
        pass


async def deliver_stream_only(m: Message, msg: Message, link: str, tag: str):
    try:
        await msg.edit_text(f"<b>📨 𝖱𝖤𝖰𝖴𝖤𝖲𝖳𝖨𝖭𝖦 𝖵𝖨𝖠 @{DISKWALADSBOT}... {tag}</b>")
        vid_msg = await fetch_via_diskwaladsbot(link)

        media = vid_msg.video or vid_msg.document
        file_name = (media.file_name if media else None) or "video.mp4"
        size = (media.file_size if media else 0) or 0

        thumb_file_id = None
        if vid_msg.video and vid_msg.video.thumbs:
            thumb_file_id = vid_msg.video.thumbs[-1].file_id
        elif vid_msg.document and vid_msg.document.thumbs:
            thumb_file_id = vid_msg.document.thumbs[-1].file_id

        # Send thumbnail as spoiler
        if thumb_file_id:
            await m.reply_photo(
                photo=thumb_file_id,
                has_spoiler=True,
                caption=f"""<b>🔒 𝖥𝖱𝖤𝖤 𝖫𝖨𝖬𝖨𝖳 𝖱𝖤𝖠𝖢𝖧𝖤𝖣 {tag}</b>

<blockquote expandable>
📂 <code>{file_name}</code>
💾 <code>{size/1048576:.2f} MB</code>
</blockquote>

𝖴𝗉𝗀𝗋𝖺𝖽𝖾 𝗍𝗈 𝖯𝗋𝖾𝗆𝗂𝗎𝗆 𝖿𝗈𝗋 𝖿𝗎𝗅𝗅 𝖽𝗈𝗐𝗇𝗅𝗈𝖺𝖽𝗌.
""",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton(
                        "💎 𝖦𝖾𝗍 𝖯𝗋𝖾𝗆𝗂𝗎𝗆",
                        callback_data="buy_premium"
                    )]
                ])
            )

            await msg.delete()

        else:
            await msg.edit_text(
                f"""<b>🔒 𝖥𝖱𝖤𝖤 𝖫𝖨𝖬𝖨𝖳 𝖱𝖤𝖠𝖢𝖧𝖤𝖣 {tag}</b>

<blockquote expandable>
📂 <code>{file_name}</code>
💾 <code>{size/1048576:.2f} MB</code>
</blockquote>""",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton(
                        "💎 𝖦𝖾𝗍 𝖯𝗋𝖾𝗆𝗂𝗎𝗆",
                        callback_data="buy_premium"
                    )]
                ])
            )

    except Exception as e:
        try:
            await msg.edit_text(f"<b>❌ 𝖤𝗋𝗋𝗈𝗋 {tag}</b>\n<code>{e}</code>")
        except:
            pass


# ── Per-link worker (full download + upload) ──────────────────────
async def process_link(app: Client, m: Message, link: str, idx: int, total: int):
    tag = f"[{idx}/{total}]"
    msg = await m.reply(f"<blockquote><b>⚡ 𝖥𝖤𝖳𝖢𝖧𝖨𝖭𝖦 𝖫𝖨𝖭𝖪 {tag}...</b></blockquote>")

    if await try_deliver_from_cache(app, m, msg, link, tag):
        return

    async with await get_link_lock(link):
        if await try_deliver_from_cache(app, m, msg, link, tag):
            return

        try:
            await msg.edit_text(f"<b>📨 𝖱𝖤𝖰𝖴𝖤𝖲𝖳𝖨𝖭𝖦 𝖵𝖨𝖠 @{DISKWALADSBOT}... {tag}</b>")
            vid_msg = await fetch_via_diskwaladsbot(link)

            media = vid_msg.video or vid_msg.document
            file_name = (media.file_name if media else None) or "video.mp4"
            actual_size = (media.file_size if media else 0) or 0
            await add_bandwidth(actual_size)

            await save_cache(link, VIDEO_STORAGE_CHANNEL, vid_msg.id, file_name, actual_size)

            dump_ids = await get_dumps()
            sent = await app.copy_message(m.chat.id, VIDEO_STORAGE_CHANNEL, vid_msg.id)
            if dump_ids:
                asyncio.create_task(copy_to_dumps(app, sent, dump_ids))

            panel = await get_panel_settings()
            await schedule_auto_delete(app, sent.chat.id, sent.id, panel["auto_delete_seconds"])

            try:
                await msg.delete()
            except Exception:
                pass

        except UserIsBlocked:
            pass

        except Exception as e:
            try:
                await msg.edit_text(f"<b>❌ 𝖤𝗋𝗋𝗈𝗋 {tag}</b>\n<code>{e}</code>")
            except Exception:
                pass

RE = re.compile(
    r"https?://(?:www\.)?(?:diskwala\.com/app/[A-Za-z0-9]+|flezen\.com/s/[A-Za-z0-9]+)",
    re.I
)


async def store_video_for_link(app: Client, link: str, status_msg: Message, tag: str):
    """Triggers @Diskwaladsbot for a link (skipping ones already cached) and
    caches the reference to the video it uploads into VIDEO_STORAGE_CHANNEL
    — no local download/re-upload needed. Populates the same shared cache
    process_link() uses, so later /start deep-link requests for this link
    are served instantly via copy instead of re-fetching."""
    if await get_cache(link):
        return  # already stored previously

    async with await get_link_lock(link):
        if await get_cache(link):
            return

        await status_msg.edit_text(f"<b>📨 𝖱𝖤𝖰𝖴𝖤𝖲𝖳𝖨𝖭𝖦 𝖵𝖨𝖠 @{DISKWALADSBOT}... {tag}</b>")
        vid_msg = await fetch_via_diskwaladsbot(link)

        media = vid_msg.video or vid_msg.document
        file_name = (media.file_name if media else None) or "video.mp4"
        actual_size = (media.file_size if media else 0) or 0
        await add_bandwidth(actual_size)

        await save_cache(link, VIDEO_STORAGE_CHANNEL, vid_msg.id, file_name, actual_size)


@Client.on_message(
    filters.private & filters.user(OWNER_ID) & (filters.photo | filters.video) & filters.caption
)
async def admin_repost(app: Client, m: Message):
    """Fast entrypoint — Pyrogram calls this per-message via a limited pool
    of worker slots (TG_BOT_WORKERS). If we did the downloading/uploading
    right here, those slots would fill up with admin posts and normal users
    clicking links would get no response until all posts finished. Instead
    we hand the real work to a detached background task and return
    immediately, freeing this worker slot right away for the next update
    (a user's link, another admin post, etc). The background tasks still
    serialize themselves via admin_repost_lock so posts don't get mixed."""
    if is_stale_message(m):
        return  # backlog from downtime — skip instead of flooding processing
    caption = m.caption or ""
    matches = list(RE.finditer(caption))
    if not matches:
        return  # no diskwala/flezen links in this post — not for us

    asyncio.create_task(_run_admin_repost(app, m, matches))
    raise StopPropagation


async def _run_admin_repost(app: Client, m: Message, matches):
    """Owner sends/forwards a photo or video post whose caption contains one
    or more Diskwala/Flezen links. The bot downloads each linked video into
    VIDEO_STORAGE_CHANNEL, builds a single combined deep-link, and reposts
    (same media, brand-new caption built from the /panel caption template —
    original caption text is discarded entirely) into every configured post
    channel (see /addpost, /delpost, /postchannels).

    Multiple posts sent back-to-back are processed strictly one at a time
    (admin_repost_lock) — post #1 fully finishes (every link downloaded,
    uploaded, and reposted) before post #2 starts, so videos never end up
    mixed between posts. Running as a background task (see admin_repost
    above) means this queueing never blocks normal users."""
    async with admin_repost_lock:
        post_channels = await get_repost_channels()
        if not VIDEO_STORAGE_CHANNEL or not post_channels:
            await m.reply(
                "⚠️ Set VIDEO_STORAGE_CHANNEL env var and add at least one "
                "post channel with /addpost first."
            )
            raise StopPropagation

        links = [mm.group(0) for mm in matches]
        total = len(links)
        status = await m.reply(f"<b>⚙️ Processing {total} link(s)...</b>")

        for i, link in enumerate(links, start=1):
            tag = f"[{i}/{total}]"
            try:
                await store_video_for_link(app, link, status, tag)
            except Exception as e:
                await status.edit_text(f"<b>❌ Failed on {tag}</b>\n<code>{e}</code>")
                raise StopPropagation

        me = await app.get_me()
        code = await create_short_code(links)
        combined_link = f"https://t.me/{me.username}?start={code}"

        panel = await get_panel_settings()
        new_caption = panel["caption_template"].format(link=combined_link)

        button_markup = None
        if panel["button_url"]:
            button_markup = InlineKeyboardMarkup(
                [[InlineKeyboardButton(panel["button_text"], url=panel["button_url"])]]
            )

        for channel in post_channels:
            try:
                if m.photo:
                    await app.send_photo(
                        channel, m.photo.file_id, caption=new_caption,
                        reply_markup=button_markup, parse_mode=ParseMode.HTML,
                    )
                elif m.video:
                    await app.send_video(
                        channel, m.video.file_id, caption=new_caption,
                        reply_markup=button_markup, parse_mode=ParseMode.HTML,
                    )
            except Exception as e:
                await status.reply(f"⚠️ Failed to post to <code>{channel}</code>: {e}")

        await status.edit_text(
            f"<b>✅ Stored {total} video(s) and reposted to {len(post_channels)} channel(s).</b>"
        )
        raise StopPropagation



@Client.on_message(filters.private & (filters.text | filters.caption) & ~filters.command(CMDS))
async def diskwala(app: Client, m: Message):
    _dab_logger.info(f"diskwala() invoked: chat={m.chat.id} from={m.from_user.id if m.from_user else '?'} text={(m.text or m.caption or '')!r}")
    if is_stale_message(m):
        _dab_logger.info("diskwala() skipped — message flagged stale")
        return  # backlog from downtime — skip instead of flooding processing
    links = RE.findall(m.text or m.caption or "")
    _dab_logger.info(f"diskwala() found links: {links}")
    if not links:
        return

    if not await is_subscribed(app, m.from_user.id):
        await m.reply(
            f"<b>🔒 𝖩𝗈𝗂𝗇 𝗈𝗎𝗋 𝖼𝗁𝖺𝗇𝗇𝖾𝗅 𝗍𝗈 𝗎𝗌𝖾 𝗍𝗁𝗂𝗌 𝖻𝗈𝗍</b>\n\n"
            f"Join @{FORCE_SUB_CHANNEL}, then send your link again.",
            reply_markup=join_prompt_markup(),
        )
        return

    uid = m.from_user.id
    premium = await is_premium(uid)
    used = await get_free_used(uid)
    total = len(links)

    tasks = []

    if premium or used < FREE_LIMIT:
        # Whole batch (1 or many links) counts as a single free use.
        for i, link in enumerate(links):
            tasks.append(process_link(app, m, link, i + 1, total))
        await asyncio.gather(*tasks, return_exceptions=True)
        if not premium:
            await increment_free_used(uid)
    else:
        for i, link in enumerate(links):
            tag = f"[{i + 1}/{total}]"
            msg = await m.reply(
                f"<blockquote><b>⚡ 𝖥𝖤𝖳𝖢𝖧𝖨𝖭𝖦 𝖫𝖨𝖭𝖪 {tag}...</b></blockquote>"
            )
            tasks.append(deliver_stream_only(m, msg, link, tag))
        await asyncio.gather(*tasks, return_exceptions=True)

from pyrogram import filters
from pyrogram.errors import FloodWait, UserIsBlocked, InputUserDeactivated, PeerIdInvalid
import asyncio

@Client.on_message(filters.command("broadcast") & filters.user(OWNER_ID))
async def broadcast(client, message):
    if not message.reply_to_message:
        return await message.reply(
            "<b>Reply to a message with <code>/broadcast</code>.</b>"
        )

    users = db.find({}, {"_id": 1})

    status = await message.reply("<b>📢 Broadcasting...</b>")

    sent = failed = blocked = deleted = 0

    async for user in users:
        uid = user["_id"]

        try:
            await message.reply_to_message.copy(uid)
            sent += 1

        except FloodWait as e:
            await asyncio.sleep(e.value)
            try:
                await message.reply_to_message.copy(uid)
                sent += 1
            except Exception:
                failed += 1

        except UserIsBlocked:
            blocked += 1

        except InputUserDeactivated:
            deleted += 1

        except PeerIdInvalid:
            failed += 1

        except Exception:
            failed += 1

    await status.edit_text(
        f"""<b>📢 Broadcast Completed</b>

✅ Sent: <code>{sent}</code>
🚫 Blocked: <code>{blocked}</code>
🗑 Deleted: <code>{deleted}</code>
❌ Failed: <code>{failed}</code>
👥 Total: <code>{sent+blocked+deleted+failed}</code>
"""
    )
