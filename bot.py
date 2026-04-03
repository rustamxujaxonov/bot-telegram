import os
import time
import asyncpg
import redis.asyncio as redis
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# Sozlamalar
TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
REDIS_URL = os.getenv("REDIS_URL")

db = None
r = None

# Haqoratli so'zlar ro'yxati
BAD_WORDS = ["axuyet", "jalab", "yiban", "so'kinish1", "so'kinish2"]

# ===== DATABASE VA REDIS INIT =====
async def init():
    global db, r
    db = await asyncpg.create_pool(DATABASE_URL)
    r = redis.from_url(REDIS_URL, decode_responses=True)

    async with db.acquire() as conn:
        await conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id BIGINT PRIMARY KEY,
            gender TEXT,
            search_pref TEXT,
            state TEXT,
            partner BIGINT,
            reports INT DEFAULT 0,
            is_banned BOOLEAN DEFAULT FALSE
        )
        """)

async def get_user(uid):
    async with db.acquire() as conn:
        return await conn.fetchrow("SELECT * FROM users WHERE user_id=$1", uid)

async def update_user(uid, **kwargs):
    cols = ", ".join([f"{k}=${i+2}" for i, k in enumerate(kwargs.keys())])
    vals = list(kwargs.values())
    async with db.acquire() as conn:
        await conn.execute(f"UPDATE users SET {cols} WHERE user_id=$1", uid, *vals)

# ===== KEYBOARD =====
def get_kb(state):
    if state == "gender":
        return ReplyKeyboardMarkup([["👨 Erkak", "👩 Ayol"]], resize_keyboard=True)
    
    if state == "menu":
        return ReplyKeyboardMarkup([
            ["🔎 Jins bo‘yicha qidirish"],
            ["🎲 Random qidirish"],
            ["🔄 Jinsni o‘zgartirish"]
        ], resize_keyboard=True)
    
    if state == "search_gender":
        return ReplyKeyboardMarkup([
            ["👨 O‘g‘il qidirish", "👩 Qiz qidirish"],
            ["🔙 Orqaga"]
        ], resize_keyboard=True)
    
    if state == "searching":
        return ReplyKeyboardMarkup([["❌ Bekor qilish"]], resize_keyboard=True)
    
    if state == "chat":
        return ReplyKeyboardMarkup([
            ["⛔ Tugatish", "🔄 Keyingi"],
            ["🚨 Shikoyat"]
        ], resize_keyboard=True)
    
    return ReplyKeyboardRemove()

# ===== MATCHMAKING LOGIC =====
async def find_match(uid, context: ContextTypes.DEFAULT_TYPE):
    user = await get_user(uid)
    my_gender = user['gender']
    pref = user['search_pref']

    # Navbat kalitlari
    queue_key = f"queue:{pref}"
    search_in = f"queue:{my_gender}" if pref != "random" else "queue:random"
    
    partner_id = await r.lpop(search_in)

    if partner_id:
        partner_id = int(partner_id)
        
        # O'zini o'zi topib olmaslik uchun tekshiruv
        if partner_id == uid:
            return False

        await update_user(uid, partner=partner_id, state="chat")
        await update_user(partner_id, partner=uid, state="chat")

        msg = "🎉 **Suhbatdosh topildi!**\n\nMarhamat, suhbatni boshlashingiz mumkin.\n\n_Tugmalar:_\n⛔ **Tugatish** - Chatni yopish\n🔄 **Keyingi** - Yangi odam qidirish\n🚨 **Shikoyat** - Bloklash uchun"
        
        await context.bot.send_message(uid, msg, reply_markup=get_kb("chat"), parse_mode="Markdown")
        await context.bot.send_message(partner_id, msg, reply_markup=get_kb("chat"), parse_mode="Markdown")
        return True
    else:
        await r.rpush(queue_key, uid)
        return False

# ===== MESSAGE HANDLER =====
async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    text = update.message.text
    
    user = await get_user(uid)
    if not user:
        return # Start bosilmagan bo'lsa

    # 1. BLOK TEKSHIRUVI
    if user['is_banned']:
        return await update.message.reply_text("🚫 Siz qoidalarni buzganingiz uchun botdan bloklangansiz!")

    # 2. HAQORAT FILTRI
    if text and any(word in text.lower() for word in BAD_WORDS):
        await update_user(uid, is_banned=True)
        return await update.message.reply_text("🚫 Haqoratli so'z ishlatganingiz uchun bloklandingiz!")

    state = user['state']

    # GENDER
    if state == "gender":
        if text in ["👨 Erkak", "👩 Ayol"]:
            gender_val = "male" if "Erkak" in text else "female"
            await update_user(uid, gender=gender_val, state="menu")
            await update.message.reply_text("✅ Jinsingiz saqlandi. Menyu:", reply_markup=get_kb("menu"))
        return

    # MENU
    if state == "menu":
        if text == "🔎 Jins bo‘yicha qidirish":
            await update_user(uid, state="search_gender")
            await update.message.reply_text("Kim bilan suhbatlashmoqchisiz?", reply_markup=get_kb("search_gender"))
        elif text == "🎲 Random qidirish":
            await update_user(uid, state="searching", search_pref="random")
            await update.message.reply_text("🔍 **Suhbatdosh qidirilyapti...**", reply_markup=get_kb("searching"), parse_mode="Markdown")
            await find_match(uid, context)
        elif text == "🔄 Jinsni o‘zgartirish":
            await update_user(uid, state="gender")
            await update.message.reply_text("Jinsingizni tanlang:", reply_markup=get_kb("gender"))
        return

    # SEARCH GENDER
    if state == "search_gender":
        if text == "🔙 Orqaga":
            await update_user(uid, state="menu")
            await update.message.reply_text("Asosiy menyu:", reply_markup=get_kb("menu"))
        elif text in ["👨 O‘g‘il qidirish", "👩 Qiz qidirish"]:
            pref = "male" if "O‘g‘il" in text else "female"
            await update_user(uid, state="searching", search_pref=pref)
            await update.message.reply_text(f"🔍 **{text} boshlandi...**", reply_markup=get_kb("searching"), parse_mode="Markdown")
            await find_match(uid, context)
        return

    # SEARCHING
    if state == "searching":
        if text == "❌ Bekor qilish":
            pref = user['search_pref']
            await r.lrem(f"queue:{pref}", 0, str(uid))
            await update_user(uid, state="menu")
            await update.message.reply_text("❌ Qidiruv to'xtatildi.", reply_markup=get_kb("menu"))
        return

    # CHAT
    if state == "chat":
        partner_id = user['partner']
        
        if text == "⛔ Tugatish":
            await update_user(uid, state="menu", partner=None)
            await update_user(partner_id, state="menu", partner=None)
            await update.message.reply_text("❌ Suhbat tugadi.", reply_markup=get_kb("menu"))
            await context.bot.send_message(partner_id, "❌ Suhbatdosh suhbatni tugatdi.", reply_markup=get_kb("menu"))
        
        elif text == "🔄 Keyingi":
            await update_user(partner_id, state="menu", partner=None)
            await context.bot.send_message(partner_id, "⚠️ Suhbatdosh boshqa chatga o'tib ketdi.", reply_markup=get_kb("menu"))
            
            await update_user(uid, state="searching")
            await update.message.reply_text("🔄 Yangi suhbatdosh qidirilyapti...", reply_markup=get_kb("searching"))
            await find_match(uid, context)

        elif text == "🚨 Shikoyat":
            new_reports = user['reports'] + 1
            await update_user(partner_id, reports=new_reports)
            
            # Agar shikoyatlar 3 tadan oshsa bloklash
            if new_reports >= 20:
                await update_user(partner_id, is_banned=True, state="menu", partner=None)
                await context.bot.send_message(partner_id, "🚫 Sizga ko'p shikoyat tushgani uchun bloklandingiz!")
            
            # Chatni darhol yopish (shikoyatdan so'ng)
            await update_user(uid, state="menu", partner=None)
            await update_user(partner_id, state="menu", partner=None)
            await update.message.reply_text("🚨 Shikoyat yuborildi va suhbat yopildi.", reply_markup=get_kb("menu"))
            await context.bot.send_message(partner_id, "❌ Suhbatdosh sizdan shikoyat qildi va chatni tark etdi.", reply_markup=get_kb("menu"))
            
        else:
            # Xabarni sherigiga yuborish
            try:
                await context.bot.send_message(partner_id, text)
            except:
                await update_user(uid, state="menu", partner=None)
                await update.message.reply_text("⚠️ Suhbatdosh botdan chiqib ketgan ko'rinadi.", reply_markup=get_kb("menu"))

# ===== START COMMAND =====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    user = await get_user(uid)
    
    if not user:
        async with db.acquire() as conn:
            await conn.execute("INSERT INTO users (user_id, state) VALUES ($1, 'gender')", uid)
        await update.message.reply_text("👋 Xush kelibsiz! Avval jinsingizni tanlang:", reply_markup=get_kb("gender"))
    else:
        if user['is_banned']:
            return await update.message.reply_text("🚫 Siz bloklangansiz!")
        await update_user(uid, state="menu", partner=None)
        await update.message.reply_text("Asosiy menyuga qaytdingiz:", reply_markup=get_kb("menu"))

# ===== RUN BOT =====
def main():
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))
    
    async def setup(application):
        await init()
    
    app.post_init = setup
    app.run_polling()

if __name__ == "__main__":
    main()