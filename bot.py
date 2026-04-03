import os
import time
import asyncpg
import redis.asyncio as redis

from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    filters, ContextTypes
)

TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
REDIS_URL = os.getenv("REDIS_URL")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
PORT = int(os.getenv("PORT", 8080))
ADMIN_ID = int(os.getenv("ADMIN_ID"))

db = None
r = None
user_last_message = {}

# ===== INIT =====
async def init():
    global db, r

    db = await asyncpg.create_pool(DATABASE_URL)
    r = redis.from_url(REDIS_URL)

    async with db.acquire() as conn:
        await conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id BIGINT PRIMARY KEY,
            gender TEXT,
            search TEXT,
            state TEXT,
            partner BIGINT,
            banned_until BIGINT DEFAULT 0,
            reports INT DEFAULT 0
        )
        """)

# ===== DB =====
async def get_user(uid):
    async with db.acquire() as conn:
        return await conn.fetchrow("SELECT * FROM users WHERE user_id=$1", uid)

async def create_user(uid):
    async with db.acquire() as conn:
        await conn.execute("""
        INSERT INTO users (user_id, state)
        VALUES ($1, 'gender')
        ON CONFLICT (user_id) DO NOTHING
        """, uid)

async def update_user(uid, field, value):
    async with db.acquire() as conn:
        await conn.execute(f"UPDATE users SET {field}=$1 WHERE user_id=$2", value, uid)

# ===== KEYBOARD =====
def kb(state):
    if state == "gender":
        return ReplyKeyboardMarkup([["👨 Erkak", "👩 Ayol"]], resize_keyboard=True)

    if state == "menu":
        return ReplyKeyboardMarkup([
            ["🎯 Jins bo‘yicha qidirish"],
            ["🎲 Random chat"],
            ["🔄 Jinsni o‘zgartirish"]
        ], resize_keyboard=True)

    if state == "choose_gender":
        return ReplyKeyboardMarkup([
            ["👨 O‘g‘il bola"],
            ["👩 Qiz bola"],
            ["🔙 Orqaga"]
        ], resize_keyboard=True)

    if state == "chat":
        return ReplyKeyboardMarkup([
            ["⛔ Stop", "🔄 Next"],
            ["🚨 Report"]
        ], resize_keyboard=True)

# ===== HELPERS =====
async def send_menu(update, text, state):
    await update.message.reply_text(text, reply_markup=kb(state))

def is_spam(uid):
    now = time.time()
    last = user_last_message.get(uid, 0)

    if now - last < 0.5:
        return True

    user_last_message[uid] = now
    return False

# ===== MATCH =====
async def find_match(uid, context):
    user = await get_user(uid)

    search = user["search"]
    gender = user["gender"]

    queue = f"queue:{search}"
    partner = await r.lpop(queue)

    if not partner and search == "any":
        # random fallback
        partner = await r.lpop("queue:male") or await r.lpop("queue:female")

    if partner:
        partner = int(partner)

        await update_user(uid, "partner", partner)
        await update_user(partner, "partner", uid)

        await update_user(uid, "state", "chat")
        await update_user(partner, "state", "chat")

        await context.bot.send_message(uid, "✅ Chat boshlandi", reply_markup=kb("chat"))
        await context.bot.send_message(partner, "✅ Chat boshlandi", reply_markup=kb("chat"))
        return True

    await r.rpush(queue, uid)
    return False

# ===== START =====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    await create_user(uid)
    await send_menu(update, "👋 Jinsni tanlang", "gender")

# ===== MAIN HANDLER =====
async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    text = update.message.text

    if is_spam(uid):
        return

    await create_user(uid)
    user = await get_user(uid)

    state = user["state"]

    # ===== GENDER =====
    if state == "gender":
        if text == "👨 Erkak":
            await update_user(uid, "gender", "male")
        elif text == "👩 Ayol":
            await update_user(uid, "gender", "female")
        else:
            return

        await update_user(uid, "state", "menu")
        return await send_menu(update, "🏠 Menyu", "menu")

    # ===== MENU =====
    elif state == "menu":

        if text == "🎯 Jins bo‘yicha qidirish":
            await update_user(uid, "state", "choose_gender")
            return await send_menu(update, "Kimni qidirasiz?", "choose_gender")

        elif text == "🎲 Random chat":
            await update_user(uid, "search", "any")
            await update_user(uid, "state", "searching")

            await update.message.reply_text("⏳ Random qidirilmoqda...")
            await find_match(uid, context)

        elif text == "🔄 Jinsni o‘zgartirish":
            await update_user(uid, "state", "gender")
            return await send_menu(update, "Jinsni tanlang", "gender")

    # ===== CHOOSE GENDER =====
    elif state == "choose_gender":

        if text == "🔙 Orqaga":
            await update_user(uid, "state", "menu")
            return await send_menu(update, "🏠 Menyu", "menu")

        elif text == "👨 O‘g‘il bola":
            await update_user(uid, "search", "male")

        elif text == "👩 Qiz bola":
            await update_user(uid, "search", "female")
        else:
            return

        await update_user(uid, "state", "searching")
        await update.message.reply_text("⏳ Qidirilmoqda...")
        await find_match(uid, context)

    # ===== CHAT =====
    elif state == "chat":
        partner = user["partner"]

        if not partner:
            await update_user(uid, "state", "menu")
            return await send_menu(update, "🏠 Menyu", "menu")

        if text == "⛔ Stop":
            await update_user(uid, "state", "menu")
            await update_user(uid, "partner", None)

            await update_user(partner, "state", "menu")
            await update_user(partner, "partner", None)

            await context.bot.send_message(partner, "❌ Chat tugadi", reply_markup=kb("menu"))
            return await send_menu(update, "❌ Tugadi", "menu")

        if text == "🔄 Next":
            await update_user(uid, "partner", None)
            await update_user(uid, "state", "searching")

            await context.bot.send_message(partner, "❌ Suhbatdosh chiqib ketdi", reply_markup=kb("menu"))

            await update_user(partner, "partner", None)
            await update_user(partner, "state", "menu")

            await update.message.reply_text("🔄 Qidirilmoqda...")
            await find_match(uid, context)
            return

        if text == "🚨 Report":
            reports = user["reports"] + 1
            await update_user(partner, "reports", reports)

            return await update.message.reply_text("🚨 Shikoyat yuborildi")

        await context.bot.send_message(partner, text)

# ===== ADMIN =====
async def stats(update, context):
    if update.effective_user.id != ADMIN_ID:
        return

    async with db.acquire() as conn:
        count = await conn.fetchval("SELECT COUNT(*) FROM users")

    await update.message.reply_text(f"👥 Users: {count}")

# ===== RUN =====
def main():
    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handler))

    async def on_start(app):
        await init()

    app.post_init = on_start

    print("🚀 BOT START...")

    if WEBHOOK_URL:
        print("🌐 WEBHOOK MODE")
        app.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=TOKEN,
            webhook_url=f"{WEBHOOK_URL}/{TOKEN}",
        )
    else:
        print("💻 POLLING MODE")
        app.run_polling()


if __name__ == "__main__":
    main()