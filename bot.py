import os
import asyncpg
import redis.asyncio as redis
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# --- SOZLAMALAR ---
TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
REDIS_URL = os.getenv("REDIS_URL")

# Haqoratli so'zlar ro'yxati
BAD_WORDS = ["axuyet", "jalab", "yiban", "so'kinish1", "so'kinish2"]

db = None
r = None

# --- DATABASE VA REDISNI ISHGA TUSHIRISH ---
async def init():
    global db, r
    # Ma'lumotlar bazasiga ulanish
    db = await asyncpg.create_pool(DATABASE_URL)
    # Redisga ulanish (decode_responses=True bo'lishi shart)
    r = redis.from_url(REDIS_URL, decode_responses=True)

    async with db.acquire() as conn:
        # Jadval yaratish (agar yo'q bo'lsa)
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
        
        # Mavjud jadvalga yangi ustunlarni xavfsiz qo'shish (KeyError oldini olish uchun)
        try:
            await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS is_banned BOOLEAN DEFAULT FALSE")
            await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS reports INT DEFAULT 0")
        except Exception as e:
            print(f"Ustun qo'shishda xatolik (ehtimol bor): {e}")

# --- YORDAMCHI FUNKSIYALAR ---
async def get_user(uid):
    async with db.acquire() as conn:
        return await conn.fetchrow("SELECT * FROM users WHERE user_id=$1", uid)

async def update_user(uid, **kwargs):
    if not kwargs: return
    cols = ", ".join([f"{k}=${i+2}" for i, k in enumerate(kwargs.keys())])
    vals = list(kwargs.values())
    async with db.acquire() as conn:
        await conn.execute(f"UPDATE users SET {cols} WHERE user_id=$1", uid, *vals)

# --- KLAVIATURA ---
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
            ["👨 O‘g‘il bola qidirish", "👩 Qiz bola qidirish"],
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

# --- QIDIRUV LOGIKASI ---
async def find_match(uid, context: ContextTypes.DEFAULT_TYPE):
    user = await get_user(uid)
    if not user: return False
    
    my_gender = user.get('gender')
    pref = user.get('search_pref')

    queue_key = f"queue:{pref}"
    # Agarda men erkak qidirsam, queue:male ichidan kutayotgan ayolni qidiraman
    search_in = f"queue:{my_gender}" if pref != "random" else "queue:random"
    
    partner_id = await r.lpop(search_in)

    if partner_id:
        partner_id = int(partner_id)
        if partner_id == uid: return False

        await update_user(uid, partner=partner_id, state="chat")
        await update_user(partner_id, partner=uid, state="chat")

        msg = "🎉 **Suhbatdosh topildi!**\n\nMarhamat, suhbatni boshlang.\n\n_Tugmalar:_\n⛔ **Tugatish** - Chatni yopish\n🔄 **Keyingi** - Yangi odam qidirish\n🚨 **Shikoyat** - Nojo'ya xatti-harakat uchun"
        
        await context.bot.send_message(uid, msg, reply_markup=get_kb("chat"), parse_mode="Markdown")
        await context.bot.send_message(partner_id, msg, reply_markup=get_kb("chat"), parse_mode="Markdown")
        return True
    else:
        await r.rpush(queue_key, uid)
        return False

# --- ASOSIY XABARLARNI QAYTA ISHLASH ---
async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    text = update.message.text
    if not text: return

    user = await get_user(uid)
    if not user:
        # Agar foydalanuvchi bazada bo'lmasa, startga yo'naltirish
        async with db.acquire() as conn:
            await conn.execute("INSERT INTO users (user_id, state) VALUES ($1, 'gender') ON CONFLICT DO NOTHING", uid)
        return await update.message.reply_text("Iltimos, jinsingizni tanlang:", reply_markup=get_kb("gender"))

    # Xavfsiz tekshiruv (KeyError oldini olish)
    if user.get('is_banned', False):
        return await update.message.reply_text("🚫 Siz qoidalarni buzganingiz uchun bloklangansiz!")

    # Haqorat filtri
    if any(word in text.lower() for word in BAD_WORDS):
        await update_user(uid, is_banned=True)
        return await update.message.reply_text("🚫 Haqoratli so'z ishlatganingiz uchun bloklandingiz!")

    state = user.get('state')

    # --- STATES ---
    if state == "gender":
        if text in ["👨 Erkak", "👩 Ayol"]:
            g = "male" if "Erkak" in text else "female"
            await update_user(uid, gender=g, state="menu")
            await update.message.reply_text("✅ Saqlandi. Menyu:", reply_markup=get_kb("menu"))
        return

    elif state == "menu":
        if text == "🔎 Jins bo‘yicha qidirish":
            await update_user(uid, state="search_gender")
            await update.message.reply_text("Kimni qidiramiz?", reply_markup=get_kb("search_gender"))
        elif text == "🎲 Random qidirish":
            await update_user(uid, state="searching", search_pref="random")
            await update.message.reply_text("🔍 **Suhbatdosh qidirilyapti...**", reply_markup=get_kb("searching"), parse_mode="Markdown")
            await find_match(uid, context)
        elif text == "🔄 Jinsni o‘zgartirish":
            await update_user(uid, state="gender")
            await update.message.reply_text("Jinsingizni tanlang:", reply_markup=get_kb("gender"))
        return

    elif state == "search_gender":
        if text == "🔙 Orqaga":
            await update_user(uid, state="menu")
            await update.message.reply_text("Asosiy menyu:", reply_markup=get_kb("menu"))
        elif text in ["👨 O‘g‘il qidirish", "👩 Qiz qidirish"]:
            pref = "male" if "O‘g‘il" in text else "female"
            await update_user(uid, state="searching", search_pref=pref)
            await update.message.reply_text("🔍 **Qidiruv boshlandi...**", reply_markup=get_kb("searching"), parse_mode="Markdown")
            await find_match(uid, context)
        return

    elif state == "searching":
        if text == "❌ Bekor qilish":
            pref = user.get('search_pref', 'random')
            await r.lrem(f"queue:{pref}", 0, str(uid))
            await update_user(uid, state="menu")
            await update.message.reply_text("❌ Qidiruv to'xtatildi.", reply_markup=get_kb("menu"))
        return

    elif state == "chat":
        partner_id = user.get('partner')
        if not partner_id:
            await update_user(uid, state="menu")
            return await update.message.reply_text("Suhbatdosh topilmadi.", reply_markup=get_kb("menu"))

        if text == "⛔ Tugatish":
            await update_user(uid, state="menu", partner=None)
            await update_user(partner_id, state="menu", partner=None)
            await update.message.reply_text("❌ Suhbat tugadi.", reply_markup=get_kb("menu"))
            await context.bot.send_message(partner_id, "❌ Suhbatdosh suhbatni tugatdi.", reply_markup=get_kb("menu"))
        
        elif text == "🔄 Keyingi":
            await update_user(partner_id, state="menu", partner=None)
            await context.bot.send_message(partner_id, "⚠️ Suhbatdosh boshqa chatga o'tdi.", reply_markup=get_kb("menu"))
            await update_user(uid, state="searching")
            await update.message.reply_text("🔄 Keyingi suhbatdosh qidirilyapti...", reply_markup=get_kb("searching"))
            await find_match(uid, context)

        elif text == "🚨 Shikoyat":
            partner_data = await get_user(partner_id)
            new_reports = partner_data.get('reports', 0) + 1
            await update_user(partner_id, reports=new_reports)
            
            if new_reports >= 20:
                await update_user(partner_id, is_banned=True, state="menu", partner=None)
                await context.bot.send_message(partner_id, "🚫 Sizga ko'p shikoyat tushgani uchun bloklandingiz!")
            
            await update_user(uid, state="menu", partner=None)
            await update_user(partner_id, state="menu", partner=None)
            await update.message.reply_text("🚨 Shikoyat yuborildi va suhbat yopildi.", reply_markup=get_kb("menu"))
            await context.bot.send_message(partner_id, "❌ Suhbatdosh shikoyat qildi va chatni tark etdi.", reply_markup=get_kb("menu"))
        else:
            try:
                await context.bot.send_message(partner_id, text)
            except:
                await update_user(uid, state="menu", partner=None)
                await update.message.reply_text("⚠️ Aloqa uzildi.", reply_markup=get_kb("menu"))

# --- START BUYRUG'I ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    user = await get_user(uid)
    
    if not user:
        async with db.acquire() as conn:
            await conn.execute("INSERT INTO users (user_id, state) VALUES ($1, 'gender') ON CONFLICT DO NOTHING", uid)
        await update.message.reply_text("👋 Anonim chatga xush kelibsiz! Jinsingizni tanlang:", reply_markup=get_kb("gender"))
    else:
        if user.get('is_banned', False):
            return await update.message.reply_text("🚫 Bloklangansiz!")
        await update_user(uid, state="menu", partner=None)
        await update.message.reply_text("Asosiy menyu:", reply_markup=get_kb("menu"))

# --- ASOSIY ISHGA TUSHIRISH ---
def main():
    if not TOKEN:
        print("BOT_TOKEN topilmadi!")
        return

    app = Application.builder().token(TOKEN).build()
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))
    
    async def on_startup(application):
        await init()
        print("🚀 Bot muvaffaqiyatli ishga tushdi!")

    app.post_init = on_startup
    app.run_polling(drop_pending_updates=True) # Conflict oldini olish uchun eski yangilanishlarni tashlab yuboradi

if __name__ == "__main__":
    main()