import os
import time
import asyncpg
import redis.asyncio as redis

from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    filters, ContextTypes
)

# ===== CONFIG =====
TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
REDIS_URL = os.getenv("REDIS_URL")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
PORT = int(os.getenv("PORT", 8080))

db = None
r = None
last_msg = {}

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
            partner BIGINT
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
        ON CONFLICT DO NOTHING
        """, uid)

async def set_user(uid, field, value):
    async with db.acquire() as conn:
        await conn.execute(f"UPDATE users SET {field}=$1 WHERE user_id=$2", value, uid)

# ===== KEYBOARDS =====
def kb_gender():
    return ReplyKeyboardMarkup([["👨 Erkak", "👩 Ayol"]], resize_keyboard=True)

def kb_menu():
    return ReplyKeyboardMarkup([
        ["🎯 Jins bo‘yicha"],
        ["🎲 Random"],
        ["🔄 Jinsni o‘zgartirish"]
    ], resize_keyboard=True)

def kb_choose():
    return ReplyKeyboardMarkup([
        ["👨 O‘g‘il bola", "👩 Qiz bola"],
        ["🔙 Orqaga"]
    ], resize_keyboard=True)

def kb_chat():
    return ReplyKeyboardMarkup([
        ["⛔ Stop", "🔄 Next"],
        ["🚨 Report"]
    ], resize_keyboard=True)

# ===== HELPERS =====
def spam(uid):
    now = time.time()
    if now - last_msg.get(uid, 0) < 0.4:
        return True
    last_msg[uid] = now
    return False

async def send(update, text, kb=None):
    await update.message.reply_text(text, reply_markup=kb)

# ===== MATCH =====
async def match(uid, context):
    user = await get_user(uid)
    search = user["search"]

    partner = await r.lpop(f"q:{search}")

    if not partner and search == "any":
        partner = await r.lpop("q:male") or await r.lpop("q:female")

    if partner:
        partner = int(partner)

        await set_user(uid, "partner", partner)
        await set_user(partner, "partner", uid)

        await set_user(uid, "state", "chat")
        await set_user(partner, "state", "chat")

        await context.bot.send_message(uid, "✅ Chat boshlandi", reply_markup=kb_chat())
        await context.bot.send_message(partner, "✅ Chat boshlandi", reply_markup=kb_chat())
        return True

    await r.rpush(f"q:{search}", uid)
    return False

# ===== START =====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    await create_user(uid)

    await send(update, "👋 Jinsni tanlang", kb_gender())

# ===== MAIN =====
async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    text = update.message.text

    if spam(uid):
        return

    await create_user(uid)
    user = await get_user(uid)
    state = user["state"]

    # ===== GENDER =====
    if state == "gender":

        if text == "👨 Erkak":
            await set_user(uid, "gender", "male")
        elif text == "👩 Ayol":
            await set_user(uid, "gender", "female")
        else:
            return

        await set_user(uid, "state", "menu")

        # 🔥 eski keyboardni olib tashlaymiz
        await update.message.reply_text("✅ Saqlandi", reply_markup=ReplyKeyboardRemove())

        return await send(update, "🏠 Menyu", kb_menu())

    # ===== MENU =====
    if state == "menu":

        if text == "🎯 Jins bo‘yicha":
            await set_user(uid, "state", "choose")
            return await send(update, "Kimni qidirasiz?", kb_choose())

        if text == "🎲 Random":
            await set_user(uid, "search", "any")
            await set_user(uid, "state", "search")

            await send(update, "⏳ Random qidirilmoqda...")
            await match(uid, context)
            return

        if text == "🔄 Jinsni o‘zgartirish":
            await set_user(uid, "state", "gender")
            return await send(update, "Jinsni tanlang", kb_gender())

    # ===== CHOOSE =====
    if state == "choose":

        if text == "🔙 Orqaga":
            await set_user(uid, "state", "menu")
            return await send(update, "🏠 Menyu", kb_menu())

        if text == "👨 O‘g‘il bola":
            await set_user(uid, "search", "male")

        elif text == "👩 Qiz bola":
            await set_user(uid, "search", "female")
        else:
            return

        await set_user(uid, "state", "search")

        await send(update, "⏳ Qidirilmoqda...")
        await match(uid, context)

    # ===== CHAT =====
    if state == "chat":
        partner = user["partner"]

        if not partner:
            await set_user(uid, "state", "menu")
            return await send(update, "🏠 Menyu", kb_menu())

        # STOP
        if text == "⛔ Stop":
            await set_user(uid, "partner", None)
            await set_user(uid, "state", "menu")

            await set_user(partner, "partner", None)
            await set_user(partner, "state", "menu")

            await context.bot.send_message(partner, "❌ Chat tugadi", reply_markup=kb_menu())
            return await send(update, "❌ Tugadi", kb_menu())

        # NEXT
        if text == "🔄 Next":
            await set_user(uid, "partner", None)
            await set_user(uid, "state", "search")

            await context.bot.send_message(partner, "❌ Suhbat tugadi", reply_markup=kb_menu())

            await set_user(partner, "partner", None)
            await set_user(partner, "state", "menu")

            await send(update, "🔄 Qidirilmoqda...")
            await match(uid, context)
            return

        # REPORT
        if text == "🚨 Report":
            await send(update, "🚨 Shikoyat yuborildi")
            return

        # MESSAGE
        await context.bot.send_message(partner, text)

# ===== RUN =====
def main():
    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handler))

    async def startup(app):
        await init()

    app.post_init = startup

    print("🚀 BOT START")

    if WEBHOOK_URL:
        print("🌐 WEBHOOK")
        app.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=TOKEN,
            webhook_url=f"{WEBHOOK_URL}/{TOKEN}",
        )
    else:
        print("💻 POLLING")
        app.run_polling()


if __name__ == "__main__":
    main()