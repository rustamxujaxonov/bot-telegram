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
ADMIN_ID = int(os.getenv("ADMIN_ID", 0))

db = None
r = None

# ===== DATABASE VA REDIS =====
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
            banned_until BIGINT DEFAULT 0,
            reports INT DEFAULT 0
        )
        """)

# ===== YORDAMCHI FUNKSIYALAR =====
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
    pref = user['search_pref'] # 'male', 'female' yoki 'random'

    # Navbat kalitini aniqlash
    # Agar men random qidirsam, 'queue:random' ga tushaman
    # Agar jins bo'yicha qidirsam (masalan qiz), 'queue:female' ga tushaman
    queue_key = f"queue:{pref}"
    
    # Hamroh qidirish
    # Agarda men 'female' qidirayotgan bo'lsam, queue:male ichidan o'zimni kutayotgan ayolni qidiraman
    search_in = f"queue:{my_gender}" if pref != "random" else "queue:random"
    
    # Redisdan bitta odamni olish
    partner_id = await r.lpop(search_in)

    if partner_id:
        partner_id = int(partner_id)
        if partner_id == uid: # O'zi chiqib qolsa (kamdan kam holat)
            return False

        # Ulash
        await update_user(uid, partner=partner_id, state="chat")
        await update_user(partner_id, partner=uid, state="chat")

        await context.bot.send_message(uid, "✅ Suhbatdosh topildi!", reply_markup=get_kb("chat"))
        await context.bot.send_message(partner_id, "✅ Suhbatdosh topildi!", reply_markup=get_kb("chat"))
        return True
    else:
        # Navbatga turish
        # Agar random bo'lsa queue:random ga, aks holda o'z jinsi bo'yicha navbatga turadi
        # Chunki qarama-qarshi jins aynan shu jinsni qidiradi
        await r.rpush(queue_key, uid)
        return False

# ===== HANDLERS =====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    user = await get_user(uid)
    
    if not user:
        async with db.acquire() as conn:
            await conn.execute("INSERT INTO users (user_id, state) VALUES ($1, 'gender')", uid)
        await update.message.reply_text("Salom! Avval jinsingizni tanlang:", reply_markup=get_kb("gender"))
    else:
        await update_user(uid, state="menu", partner=None)
        await update.message.reply_text("Asosiy menu:", reply_markup=get_kb("menu"))

async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    text = update.message.text
    user = await get_user(uid)

    if not user: return
    state = user['state']

    # 1. JINS TANLASH
    if state == "gender":
        if text == "👨 Erkak":
            await update_user(uid, gender="male", state="menu")
            await update.message.reply_text("Profil saqlandi. Menyu:", reply_markup=get_kb("menu"))
        elif text == "👩 Ayol":
            await update_user(uid, gender="female", state="menu")
            await update.message.reply_text("Profil saqlandi. Menyu:", reply_markup=get_kb("menu"))
        return

    # 2. MENU
    if state == "menu":
        if text == "🔎 Jins bo‘yicha qidirish":
            await update_user(uid, state="search_gender")
            await update.message.reply_text("Kimni qidiramiz?", reply_markup=get_kb("search_gender"))
        elif text == "🎲 Random qidirish":
            await update_user(uid, state="searching", search_pref="random")
            await update.message.reply_text("🎲 Hamroh qidirilmoqda...", reply_markup=get_kb("searching"))
            await find_match(uid, context)
        elif text == "🔄 Jinsni o‘zgartirish":
            await update_user(uid, state="gender")
            await update.message.reply_text("Jinsingizni qayta tanlang:", reply_markup=get_kb("gender"))
        return

    # 3. JINS BO'YICHA QIDIRUV TANLOVI
    if state == "search_gender":
        if text == "🔙 Orqaga":
            await update_user(uid, state="menu")
            await update.message.reply_text("Menyu:", reply_markup=get_kb("menu"))
        elif text in ["👨 O‘g‘il qidirish", "👩 Qiz qidirish"]:
            pref = "male" if "O‘g‘il" in text else "female"
            await update_user(uid, state="searching", search_pref=pref)
            await update.message.reply_text("🔎 Mos hamroh qidirilmoqda...", reply_markup=get_kb("searching"))
            await find_match(uid, context)
        return

    # 4. QIDIRUV JARAYONI
    if state == "searching":
        if text == "❌ Bekor qilish":
            pref = user['search_pref']
            await r.lrem(f"queue:{pref}", 0, str(uid)) # Navbatdan o'chirish
            await update_user(uid, state="menu")
            await update.message.reply_text("Qidiruv bekor qilindi.", reply_markup=get_kb("menu"))
        return

    # 5. CHAT JARAYONI
    if state == "chat":
        partner_id = user['partner']
        
        if text == "⛔ Tugatish":
            await update_user(uid, state="menu", partner=None)
            await update_user(partner_id, state="menu", partner=None)
            await update.message.reply_text("Suhbat tugatildi.", reply_markup=get_kb("menu"))
            await context.bot.send_message(partner_id, "Suhbatdosh suhbatni tugatdi.", reply_markup=get_kb("menu"))
        
        elif text == "🔄 Keyingi":
            # Hamkorni xabardor qilish
            await update_user(partner_id, state="menu", partner=None)
            await context.bot.send_message(partner_id, "Suhbatdosh keyingi suhbatga o'tib ketdi.", reply_markup=get_kb("menu"))
            
            # O'zini qayta qidiruvga berish
            await update_user(uid, state="searching")
            await update.message.reply_text("Yangi hamroh qidirilmoqda...", reply_markup=get_kb("searching"))
            await find_match(uid, context)

        elif text == "🚨 Shikoyat":
            await update_user(partner_id, reports=user['reports']+1)
            await update.message.reply_text("Shikoyat qabul qilindi.")
            
        else:
            # Xabarni yuborish
            try:
                await context.bot.send_message(partner_id, text)
            except:
                await update_user(uid, state="menu", partner=None)
                await update.message.reply_text("Suhbatdosh botdan chiqib ketgan ko'rinadi.", reply_markup=get_kb("menu"))

# ===== ASOSIY ISHGA TUSHIRISH =====
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