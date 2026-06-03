import os
import logging
import asyncio
import asyncpg
import redis.asyncio as redis
from datetime import datetime, timedelta, timezone

from telegram import (
    Update, ReplyKeyboardMarkup, ReplyKeyboardRemove,
    InlineKeyboardButton, InlineKeyboardMarkup,
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    filters, ContextTypes, CallbackQueryHandler,
)
from telegram.error import TelegramError, Forbidden

# ─────────────────────────── LOGGING ───────────────────────────
logging.basicConfig(
    format="%(asctime)s | %(levelname)-8s | %(name)s — %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ─────────────────────────── SOZLAMALAR ────────────────────────
TOKEN        = os.getenv("BOT_TOKEN", "")
DATABASE_URL = os.getenv("DATABASE_URL", "")
REDIS_URL    = os.getenv("REDIS_URL", "redis://localhost:6379")

ADMIN_IDS    = {int(x) for x in os.getenv("ADMIN_IDS", "1442214910").split(",")}
CARD_NUMBER  = os.getenv("CARD_NUMBER", "5614682115991368")
CARD_HOLDER  = os.getenv("CARD_HOLDER", "Rustamxon Xujaxonov")
VIP_PRICE    = int(os.getenv("VIP_PRICE", "10000"))          # UZS

REPORT_LIMIT = 5

# ─────────────────────────── GLOBAL POOL ───────────────────────
db: asyncpg.Pool | None = None
r:  redis.Redis  | None = None

# ══════════════════════════════════════════════════════════════
#  DATABASE
# ══════════════════════════════════════════════════════════════
SQL_CREATE = """
CREATE TABLE IF NOT EXISTS users (
    user_id      BIGINT  PRIMARY KEY,
    username     TEXT,
    gender       TEXT,
    search_pref  TEXT    DEFAULT 'random',
    state        TEXT    DEFAULT 'gender',
    partner      BIGINT,
    reports      INT     DEFAULT 0,
    is_banned    BOOLEAN DEFAULT FALSE,
    is_premium   BOOLEAN DEFAULT FALSE,
    premium_until TIMESTAMPTZ,
    created_at   TIMESTAMPTZ DEFAULT NOW()
);
"""

async def init_db() -> None:
    global db, r
    db = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=10)
    r  = redis.from_url(REDIS_URL, decode_responses=True)

    async with db.acquire() as conn:
        await conn.execute(SQL_CREATE)

        migrations = [
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS username TEXT",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS premium_until TIMESTAMPTZ",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT NOW()",
        ]
        for m in migrations:
            try:
                await conn.execute(m)
            except Exception:
                pass

    logger.info("✅ Database va Redis tayyor")


async def get_user(uid: int) -> asyncpg.Record | None:
    async with db.acquire() as conn:
        return await conn.fetchrow("SELECT * FROM users WHERE user_id=$1", uid)


async def upsert_user(uid: int, username: str | None = None) -> None:
    async with db.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO users (user_id, username) VALUES ($1, $2)
            ON CONFLICT (user_id) DO UPDATE SET username=EXCLUDED.username
            """,
            uid, username,
        )


async def update_user(uid: int, **kwargs) -> None:
    if not kwargs:
        return
    
    # Bugungi kunni Timezone (UTC) bilan olish
    if "is_premium" in kwargs and kwargs["is_premium"]:
        kwargs.setdefault("premium_until", datetime.now(timezone.utc) + timedelta(days=30))

    cols = ", ".join(f"{k}=${i+2}" for i, k in enumerate(kwargs))
    vals = list(kwargs.values())
    async with db.acquire() as conn:
        await conn.execute(f"UPDATE users SET {cols} WHERE user_id=$1", uid, *vals)


async def is_premium_active(user: asyncpg.Record) -> bool:
    """VIP muddati tugaganini avtomatik tekshiradi."""
    if not user["is_premium"]:
        return False
    # Timezone-aware solishtirish (Xatolik oldi olindi)
    if user["premium_until"] and user["premium_until"] < datetime.now(timezone.utc):
        await update_user(user["user_id"], is_premium=False, premium_until=None)
        return False
    return True


async def get_stats() -> dict:
    async with db.acquire() as conn:
        total   = await conn.fetchval("SELECT COUNT(*) FROM users")
        premium = await conn.fetchval("SELECT COUNT(*) FROM users WHERE is_premium=TRUE")
        banned  = await conn.fetchval("SELECT COUNT(*) FROM users WHERE is_banned=TRUE")
        today   = await conn.fetchval(
            "SELECT COUNT(*) FROM users WHERE created_at > NOW() - INTERVAL '1 day'"
        )
    active_search = await r.llen("queue:random") + await r.llen("queue:male") + await r.llen("queue:female")
    return {
        "total": total, "premium": premium,
        "banned": banned, "today": today,
        "searching": active_search,
    }


# ══════════════════════════════════════════════════════════════
#  KLAVIATURA
# ══════════════════════════════════════════════════════════════
def kb(state: str, premium: bool = False) -> ReplyKeyboardMarkup | ReplyKeyboardRemove:
    if state == "gender":
        return ReplyKeyboardMarkup([["👨 Erkak", "👩 Ayol"]], resize_keyboard=True)

    if state == "menu":
        gender_btn = "🎯 Jins bo'yicha qidirish" if premium else "🎯 Jins bo'yicha (⭐ VIP)"
        return ReplyKeyboardMarkup([
            ["🎲 Tasodifiy qidirish"],
            [gender_btn],
            ["🔄 Jinsni o'zgartirish", "💎 VIP sotib olish"],
            ["📊 Statistika", "ℹ️ Yordam"],
        ], resize_keyboard=True)

    if state == "search_gender":
        return ReplyKeyboardMarkup([
            ["👨 Yigit qidirish", "👩 Qiz qidirish"],
            ["🔙 Orqaga"],
        ], resize_keyboard=True)

    if state == "searching":
        return ReplyKeyboardMarkup([["❌ Bekor qilish"]], resize_keyboard=True)

    if state == "chat":
        return ReplyKeyboardMarkup([
            ["⛔ Tugatish", "🔄 Keyingisi"],
            ["🚨 Shikoyat"],
        ], resize_keyboard=True)

    return ReplyKeyboardRemove()


# ══════════════════════════════════════════════════════════════
#  MATCHMAKING
# ══════════════════════════════════════════════════════════════
async def find_match(uid: int, context: ContextTypes.DEFAULT_TYPE, depth: int = 0) -> bool:
    if depth > 10:
        return False

    user = await get_user(uid)
    if not user or user["state"] not in ("searching",):
        return False

    pref = user["search_pref"]
    if pref == "random":
        search_queue = "queue:random"
        my_queue     = "queue:random"
    else:
        opposite = "female" if pref == "male" else "male"
        search_queue = f"queue:{opposite}"
        my_queue     = f"queue:{user['gender']}"

    raw = await r.lpop(search_queue)

    if not raw:
        await r.rpush(my_queue, str(uid))
        return False

    partner_id = int(raw)
    if partner_id == uid:
        await r.rpush(my_queue, str(uid))
        return False

    partner = await get_user(partner_id)
    if not partner or partner["state"] != "searching":
        return await find_match(uid, context, depth + 1)

    await asyncio.gather(
        update_user(uid,        partner=partner_id, state="chat"),
        update_user(partner_id, partner=uid,        state="chat"),
    )

    hello = (
        "🎉 *Suhbatdosh topildi!*\n\n"
        "Salom deb yozing va suhbatni boshlang 😊\n"
        "_Xavfsiz suhbatlashing — rasmlar va xabarlar yuborishingiz mumkin._"
    )
    prem_partner = await is_premium_active(partner)

    await asyncio.gather(
        context.bot.send_message(uid,        hello, reply_markup=kb("chat"), parse_mode="Markdown"),
        context.bot.send_message(partner_id, hello, reply_markup=kb("chat", prem_partner), parse_mode="Markdown"),
    )
    return True


async def safe_send(bot, chat_id: int, **kwargs) -> bool:
    try:
        await bot.send_message(chat_id, **kwargs)
        return True
    except Forbidden:
        logger.warning(f"User {chat_id} botni bloklagan")
        await update_user(chat_id, state="menu", partner=None)
        return False
    except TelegramError as e:
        logger.error(f"Telegram xatolik {chat_id}: {e}")
        return False


async def safe_copy(bot, from_chat_id: int, to_chat_id: int, message_id: int) -> bool:
    """Xabarlarni (matn, rasm, stiker, audio) xavfsiz nusxalash funksiyasi"""
    try:
        await bot.copy_message(chat_id=to_chat_id, from_chat_id=from_chat_id, message_id=message_id)
        return True
    except Forbidden:
        logger.warning(f"User {to_chat_id} botni bloklagan")
        await update_user(to_chat_id, state="menu", partner=None)
        return False
    except TelegramError as e:
        logger.error(f"Copy xatolik {to_chat_id}: {e}")
        return False


# ══════════════════════════════════════════════════════════════
#  HANDLERS
# ══════════════════════════════════════════════════════════════
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid  = update.effective_user.id
    uname = update.effective_user.username

    await upsert_user(uid, uname)
    user = await get_user(uid)

    if user["is_banned"]:
        await update.message.reply_text("🚫 Siz tizimdan bloklangansiz.")
        return

    if not user["gender"]:
        await update_user(uid, state="gender")
        await update.message.reply_text(
            "👋 *Xush kelibsiz!*\n\nAnonim chat botga hush kelibsiz.\nAvval jinsingizni tanlang:",
            reply_markup=kb("gender"), parse_mode="Markdown",
        )
    else:
        old_partner = user["partner"]
        if old_partner:
            await update_user(old_partner, state="menu", partner=None)
            await safe_send(context.bot, old_partner, text="⚠️ Suhbatdoshingiz qayta ulanish uchun /start bosdi.")
        await update_user(uid, state="menu", partner=None)
        prem = await is_premium_active(user)
        await update.message.reply_text("🏠 Asosiy menyu:", reply_markup=kb("menu", prem))


async def cmd_ban(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id not in ADMIN_IDS:
        return
    if not context.args:
        await update.message.reply_text("Ishlatish: /ban <user_id>")
        return
    target = int(context.args[0])
    await update_user(target, is_banned=True, state="menu", partner=None)
    await update.message.reply_text(f"✅ {target} bloklandi.")


async def cmd_unban(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id not in ADMIN_IDS:
        return
    if not context.args:
        await update.message.reply_text("Ishlatish: /unban <user_id>")
        return
    target = int(context.args[0])
    await update_user(target, is_banned=False, reports=0)
    await update.message.reply_text(f"✅ {target} blokdan chiqarildi.")


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id not in ADMIN_IDS:
        return
    s = await get_stats()
    await update.message.reply_text(
        f"📊 *Bot statistikasi*\n\n"
        f"👥 Jami foydalanuvchilar: {s['total']}\n"
        f"⭐ VIP: {s['premium']}\n"
        f"🚫 Bloklangan: {s['banned']}\n"
        f"🆕 Bugun qo'shilgan: {s['today']}\n"
        f"🔍 Hozir qidirmoqda: {s['searching']}",
        parse_mode="Markdown",
    )


async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid  = update.effective_user.id
    msg  = update.message
    text = msg.text or ""

    user = await get_user(uid)
    if not user:
        await cmd_start(update, context)
        return
    if user["is_banned"]:
        await msg.reply_text("🚫 Siz bloklangansiz.")
        return

    state = user["state"]
    prem  = await is_premium_active(user)

    # ── To'lov cheki (rasm) ─────────────────────────────────────
    if state == "waiting_check" and msg.photo:
        photo_id = msg.photo[-1].file_id
        await update_user(uid, state="menu")
        await msg.reply_text(
            "✅ Chek qabul qilindi. Admin tasdiqlashini kuting (odatda 5–30 daqiqa).",
            reply_markup=kb("menu", prem),
        )
        admin_kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Tasdiqlash", callback_data=f"vip_accept_{uid}"),
            InlineKeyboardButton("❌ Rad etish",  callback_data=f"vip_reject_{uid}"),
        ]])
        uname = update.effective_user.username or "—"
        for admin in ADMIN_IDS:
            try:
                await context.bot.send_photo(
                    admin, photo_id,
                    caption=f"💰 *VIP to'lov so'rovi*\n\nUser ID: `{uid}`\nUsername: @{uname}",
                    reply_markup=admin_kb, parse_mode="Markdown",
                )
            except TelegramError:
                pass
        return

    # ── GENDER tanlash ──────────────────────────────────────────
    if state == "gender":
        if text in ("👨 Erkak", "👩 Ayol"):
            gender_val = "male" if "Erkak" in text else "female"
            await update_user(uid, gender=gender_val, state="menu")
            await msg.reply_text("✅ Saqlandi! Endi kimni qidiramiz?", reply_markup=kb("menu", prem))
        return

    # ── ASOSIY MENYU ────────────────────────────────────────────
    if state == "menu":
        if text == "🎲 Tasodifiy qidirish":
            await update_user(uid, state="searching", search_pref="random")
            await msg.reply_text("🔍 Suhbatdosh qidirilmoqda...", reply_markup=kb("searching"))
            found = await find_match(uid, context)
            if not found:
                await msg.reply_text("⏳ Navbatda kutilmoqda. Suhbatdosh topilishi bilanoq xabar beriladi.")

        elif "Jins bo'yicha" in text:
            if not prem:
                await msg.reply_text(
                    "⭐ Bu funksiya faqat *VIP* a'zolar uchun!\n\n"
                    "💎 VIP sotib olish uchun quyidagi tugmani bosing.",
                    reply_markup=kb("menu", False), parse_mode="Markdown",
                )
            else:
                await update_user(uid, state="search_gender")
                await msg.reply_text("Kimni qidiramiz?", reply_markup=kb("search_gender"))

        elif text == "💎 VIP sotib olish":
            await update_user(uid, state="waiting_check")
            await msg.reply_text(
                f"💎 *VIP Obuna — {VIP_PRICE:,} UZS / 30 kun*\n\n"
                f"*Imkoniyatlar:*\n"
                f"• 🎯 Jins bo'yicha qidirish\n"
                f"• ⚡ Tezkor navbat\n\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"💳 Karta: `{CARD_NUMBER}`\n"
                f"👤 Ega: {CARD_HOLDER}\n"
                f"━━━━━━━━━━━━━━━━━━\n\n"
                f"To'lovni amalga oshirib, *chekni (screenshot/rasm)* shu yerga yuboring.",
                parse_mode="Markdown",
                reply_markup=ReplyKeyboardMarkup([["🔙 Bekor qilish"]], resize_keyboard=True),
            )

        elif text == "🔄 Jinsni o'zgartirish":
            await update_user(uid, state="gender")
            await msg.reply_text("Yangi jinsingizni tanlang:", reply_markup=kb("gender"))

        elif text == "📊 Statistika":
            s = await get_stats()
            await msg.reply_text(
                f"📊 *Umumiy statistika*\n\n"
                f"👥 Foydalanuvchilar: {s['total']}\n"
                f"🔍 Hozir qidirmoqda: {s['searching']}",
                parse_mode="Markdown",
            )

        elif text == "ℹ️ Yordam":
            await msg.reply_text(
                "ℹ️ *Yordam*\n\n"
                "🎲 *Tasodifiy qidirish* — istalgan kishi bilan suhbat\n"
                "🎯 *Jins bo'yicha* — VIP foydalanuvchilar uchun\n"
                "⛔ *Tugatish* — suhbatni yakunlash\n"
                "🔄 *Keyingisi* — yangi suhbatdosh\n"
                "🚨 *Shikoyat* — noto'g'ri xulq uchun\n\n"
                "⚠️ Shaxsiy ma'lumotlaringizni ulashmang!",
                parse_mode="Markdown",
            )

        elif text == "🔙 Bekor qilish":
            await update_user(uid, state="menu")
            await msg.reply_text("Bekor qilindi.", reply_markup=kb("menu", prem))

        return

    # ── JINS BO'YICHA QIDIRUV ───────────────────────────────────
    if state == "search_gender":
        if text == "🔙 Orqaga":
            await update_user(uid, state="menu")
            await msg.reply_text("🏠 Menyu:", reply_markup=kb("menu", prem))
        elif text in ("👨 Yigit qidirish", "👩 Qiz qidirish"):
            pref = "male" if "Yigit" in text else "female"
            await update_user(uid, state="searching", search_pref=pref)
            await msg.reply_text("🔍 Qidirilmoqda...", reply_markup=kb("searching"))
            found = await find_match(uid, context)
            if not found:
                await msg.reply_text("⏳ Navbatda kutilmoqda...")
        return

    # ── QIDIRUV ─────────────────────────────────────────────────
    if state == "searching":
        if text == "❌ Bekor qilish":
            pref = user["search_pref"]
            q    = f"queue:{user['gender']}" if pref != "random" else "queue:random"
            await r.lrem(q, 0, str(uid))
            await update_user(uid, state="menu")
            await msg.reply_text("✅ Qidiruv bekor qilindi.", reply_markup=kb("menu", prem))
        return

    # ── CHAT (Barcha turdagi mediani qo'llaydi) ───────────────────
    if state == "chat":
        p_id = user["partner"]
        if not p_id:
            await update_user(uid, state="menu")
            await msg.reply_text("⚠️ Suhbat topilmadi.", reply_markup=kb("menu", prem))
            return

        partner = await get_user(p_id)
        prem_p  = await is_premium_active(partner) if partner else False

        if text == "⛔ Tugatish":
            await asyncio.gather(
                update_user(uid,  state="menu", partner=None),
                update_user(p_id, state="menu", partner=None),
            )
            await msg.reply_text("❌ Suhbat tugatildi.", reply_markup=kb("menu", prem))
            await safe_send(context.bot, p_id, text="❌ Suhbatdosh suhbatni tugatdi.", reply_markup=kb("menu", prem_p))

        elif text == "🔄 Keyingisi":
            await update_user(p_id, state="menu", partner=None)
            await safe_send(context.bot, p_id, text="⚠️ Suhbatdosh boshqa chatga o'tdi.", reply_markup=kb("menu", prem_p))
            await update_user(uid, state="searching", partner=None)
            await msg.reply_text("🔄 Yangi suhbatdosh qidirilmoqda...", reply_markup=kb("searching"))
            found = await find_match(uid, context)
            if not found:
                await msg.reply_text("⏳ Navbatda kutilmoqda...")

        elif text == "🚨 Shikoyat":
            new_reports = (partner["reports"] if partner else 0) + 1
            auto_ban    = new_reports >= REPORT_LIMIT
            await asyncio.gather(
                update_user(uid,  state="menu", partner=None),
                update_user(p_id, state="menu", partner=None,
                            reports=new_reports,
                            is_banned=auto_ban),
            )
            await msg.reply_text("🚨 Shikoyat qabul qilindi. Rahmat!", reply_markup=kb("menu", prem))
            await safe_send(context.bot, p_id, text="⚠️ Suhbat tugatildi.", reply_markup=kb("menu", prem_p))
            if auto_ban:
                logger.info(f"User {p_id} avtomatik bloklandi ({new_reports} shikoyat)")
                for admin in ADMIN_IDS:
                    await safe_send(context.bot, admin,
                                    text=f"🤖 User {p_id} avtomatik bloklandi ({new_reports} shikoyat).")

        else:
            # COPY_MESSAGE yordamida rasm, video, ovoz va stikerlarni xavfsiz o'tkazish
            sent = await safe_copy(context.bot, from_chat_id=uid, to_chat_id=p_id, message_id=msg.message_id)
            if not sent:
                await update_user(uid, state="menu", partner=None)
                await msg.reply_text("⚠️ Suhbatdosh aloqasi uzildi.", reply_markup=kb("menu", prem))


# ══════════════════════════════════════════════════════════════
#  CALLBACK QUERY (admin tugmalari)
# ══════════════════════════════════════════════════════════════
async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data

    parts = data.split("_")
    if len(parts) >= 3 and parts[0] == "vip":
        action    = parts[1]
        target_id = int(parts[2])

        if action == "accept":
            await update_user(target_id, is_premium=True)
            await safe_send(
                context.bot, target_id,
                text="🎉 *Tabriklaymiz!* To'lovingiz tasdiqlandi.\n\n⭐ Endi siz 30 kunlik *VIP* a'zosiz!",
                parse_mode="Markdown",
            )
            try:
                await query.edit_message_caption(caption="✅ VIP tasdiqlandi ✅")
            except TelegramError:
                pass

        elif action == "reject":
            await safe_send(
                context.bot, target_id,
                text="❌ Uzr, to'lovingiz tasdiqlanmadi.\nQayta urinib ko'ring yoki admin bilan bog'laning.",
            )
            try:
                await query.edit_message_caption(caption="❌ Rad etildi")
            except TelegramError:
                pass


# ══════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════
def main() -> None:
    if not TOKEN:
        raise ValueError("BOT_TOKEN muhit o'zgaruvchisi topilmadi!")

    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start",  cmd_start))
    app.add_handler(CommandHandler("ban",    cmd_ban))
    app.add_handler(CommandHandler("unban",  cmd_unban))
    app.add_handler(CommandHandler("stats",  cmd_stats))

    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, message_handler))

    async def post_init(application: Application) -> None:
        await init_db()
        await application.bot.delete_webhook(drop_pending_updates=True)

        cmds = [
            ("start", "Botni ishga tushirish"),
            ("stats", "Statistika (admin)"),
            ("ban",   "Userni bloklash (admin)"),
            ("unban", "Blokdan chiqarish (admin)"),
        ]
        await application.bot.set_my_commands(cmds)
        logger.info("🚀 Bot ishga tushdi!")

    app.post_init = post_init
    app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
