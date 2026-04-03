import os
import time
import asyncio
import asyncpg
import redis.asyncio as redis
from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
REDIS_URL = os.getenv("REDIS_URL")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
PORT = int(os.getenv("PORT", 8080))
ADMIN_ID = int(os.getenv("ADMIN_ID"))

db = None
r = None

user_last_message = {}

bad_words = ["axuyet", "jalab", "yiban", "sex", "porn"]

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

# ===== USER =====
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
            ["👨 O‘g‘il bola"],
            ["👩 Qiz bola"]
        ], resize_keyboard=True)

    if state == "chat":
        return ReplyKeyboardMarkup([
            ["⛔ Stop", "🔄 Next"],
            ["🚨 Report"]
        ], resize_keyboard=True)

# ===== SPAM =====
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

    if partner:
        partner = int(partner)

        await update_user(uid, "partner", partner)
        await update_user(partner, "partner", uid)

        await update_user(uid, "state", "chat")
        await update_user(partner, "state", "chat")

        await context.bot.send_message(uid, "✅ Chat boshlandi", reply_markup=kb("chat"))
        await context.bot.send_message(partner, "✅ Chat boshlandi", reply_markup=kb("chat"))
        return True

    await r.rpush(f"queue:{gender}", uid)
    return False

# ===== START =====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    await create_user(uid)

    await update.message.reply_text("👋 Jins tanlang", reply_markup=kb("gender"))

# ===== MAIN =====
async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    text = update.message.text

    if is_spam(uid):
        return

    await create_user(uid)
    user = await get_user(uid)

    # BAN CHECK
    if user["banned_until"] > int(time.time()):
        return await update.message.reply_text("🚫 Siz vaqtincha bloklangansiz")

    if any(w in text.lower() for w in bad_words):
        await update_user(uid, "banned_until", int(time.time()) + 86400)
        return await update.message.reply_text("🚫 24 soatga bloklandingiz")

    state = user["state"]

    # GENDER
    if state == "gender":
        if text == "👨 Erkak":
            await update_user(uid, "gender", "male")
        elif text == "👩 Ayol":
            await update_user(uid, "gender", "female")
        else:
            return

        await update_user(uid, "state", "menu")
        return await update.message.reply_text("Kimni qidirasiz?", reply_markup=kb("menu"))

    # MENU
    elif state == "menu":
        if text == "👨 O‘g‘il bola":
            await update_user(uid, "search", "male")
        elif text == "👩 Qiz bola":
            await update_user(uid, "search", "female")
        else:
            return

        await update_user(uid, "state", "searching")
        await update.message.reply_text("⏳ Qidirilmoqda...")

        found = await find_match(uid, context)
        if not found:
            await update.message.reply_text("⌛ Kutilmoqda...")

    # CHAT
    elif state == "chat":
        partner = user["partner"]

        if not partner:
            await update_user(uid, "state", "menu")
            return

        # STOP
        if text == "⛔ Stop":
            await update_user(uid, "state", "menu")
            await update_user(uid, "partner", None)

            await update_user(partner, "state", "menu")
            await update_user(partner, "partner", None)

            await context.bot.send_message(partner, "❌ Chat tugadi", reply_markup=kb("menu"))
            return await update.message.reply_text("❌ Tugadi", reply_markup=kb("menu"))

        # NEXT
        if text == "🔄 Next":
            await update_user(uid, "partner", None)
            await update_user(uid, "state", "searching")

            await context.bot.send_message(partner, "❌ Suhbatdosh chiqib ketdi", reply_markup=kb("menu"))
            await update_user(partner, "partner", None)
            await update_user(partner, "state", "menu")

            await update.message.reply_text("🔄 Qidirilmoqda...")
            await find_match(uid, context)
            return

        # REPORT
        if text == "🚨 Report":
            reports = user["reports"] + 1
            await update_user(partner, "reports", reports)

            if reports >= 20:
                await update_user(partner, "banned_until", int(time.time()) + 86400)
                await context.bot.send_message(partner, "🚫 24 soatga bloklandingiz")

            return await update.message.reply_text("🚨 Shikoyat yuborildi")

        # MESSAGE
        await context.bot.send_message(partner, text)

# ===== ADMIN =====
async def stats(update, context):
    if update.effective_user.id != ADMIN_ID:
        return

    async with db.acquire() as conn:
        count = await conn.fetchval("SELECT COUNT(*) FROM users")

    await update.message.reply_text(f"👥 Users: {count}")

# ===== RUN =====
async def main():
    await init()

    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handler))

    await app.bot.set_webhook(f"{WEBHOOK_URL}/{TOKEN}")

    await app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path=TOKEN
    )

if __name__ == "__main__":
    asyncio.run(main())
