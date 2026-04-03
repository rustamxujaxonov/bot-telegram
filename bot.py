import os
import logging
import asyncpg
import redis.asyncio as redis
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# Loggingni sozlash (xatoliklarni terminalda ko'rish uchun)
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# --- SOZLAMALAR ---
TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
REDIS_URL = os.getenv("REDIS_URL")

# Haqoratli so'zlar (kengaytirishingiz mumkin)
BAD_WORDS = ["fisiisnknskdnsksndsiskcn"]

db = None
r = None

# ===== DATABASE VA REDIS =====
async def init():
    global db, r
    try:
        db = await asyncpg.create_pool(DATABASE_URL)
        r = redis.from_url(REDIS_URL, decode_responses=True)

        async with db.acquire() as conn:
            # Jadvalni yaratish
            await conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                gender TEXT,
                search_pref TEXT DEFAULT 'random',
                state TEXT DEFAULT 'gender',
                partner BIGINT,
                reports INT DEFAULT 0,
                is_banned BOOLEAN DEFAULT FALSE
            )
            """)
            # Ustunlarni tekshirish va qo'shish (Error oldini olish)
            for col in [
                ("search_pref", "TEXT DEFAULT 'random'"),
                ("is_banned", "BOOLEAN DEFAULT FALSE"),
                ("reports", "INT DEFAULT 0")
            ]:
                try:
                    await conn.execute(f"ALTER TABLE users ADD COLUMN IF NOT EXISTS {col[0]} {col[1]}")
                except:
                    pass
        logger.info("✅ Baza muvaffaqiyatli ulandi va yangilandi!")
    except Exception as e:
        logger.error(f"❌ Bazada xatolik: {e}")

async def get_user(uid):
    async with db.acquire() as conn:
        return await conn.fetchrow("SELECT * FROM users WHERE user_id=$1", uid)

async def update_user(uid, **kwargs):
    if not kwargs: return
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
            ["🎲 Random qidirish"],
            ["🔎 Jins bo‘yicha qidirish"],
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

# ===== MATCHMAKING (Qidiruv mantiqi) =====
async def find_match(uid, context: ContextTypes.DEFAULT_TYPE):
    user = await get_user(uid)
    if not user: return False
    
    my_gender = user['gender']
    pref = user['search_pref']

    # Navbat kalitlari
    # Men o'g'il qidirsam, queue:male ichidagilarni qidiraman
    search_key = f"queue:{pref}" 
    # Men o'zim qaysi navbatda turaman
    my_queue_key = f"queue:{my_gender}" if pref != "random" else "queue:random"
    
    # Sherik qidirish
    partner_id = await r.lpop(search_key)

    if partner_id:
        partner_id = int(partner_id)
        if partner_id == uid: # O'zi bilan o'zi tushib qolsa
            await r.rpush(my_queue_key, uid)
            return False
            
        # Bazada bandligini tekshirish
        p_data = await get_user(partner_id)
        if not p_data or p_data['state'] != "searching":
            return await find_match(uid, context) # Qayta qidirish

        await update_user(uid, partner=partner_id, state="chat")
        await update_user(partner_id, partner=uid, state="chat")

        msg = "🎉 **Suhbatdosh topildi!**\n\nMarhamat, suhbatni boshlang.\n\n_Tugmalar:_\n⛔ **Tugatish** - Chatni yopish\n🔄 **Keyingi** - Yangi odam\n🚨 **Shikoyat** - Bloklash"
        await context.bot.send_message(uid, msg, reply_markup=get_kb("chat"), parse_mode="Markdown")
        await context.bot.send_message(partner_id, msg, reply_markup=get_kb("chat"), parse_mode="Markdown")
        return True
    else:
        # Hech kim yo'q bo'lsa, navbatga qo'shish
        queue_to_join = f"queue:{my_gender}" if pref != "random" else "queue:random"
        await r.rpush(queue_to_join, uid)
        return False

# ===== ASOSIY HANDLERS =====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    user = await get_user(uid)
    
    if not user:
        async with db.acquire() as conn:
            await conn.execute("INSERT INTO users (user_id, state) VALUES ($1, 'gender') ON CONFLICT DO NOTHING", uid)
        await update.message.reply_text("👋 **Anonim Chatga xush kelibsiz!**\n\nAvval jinsingizni tanlang:", reply_markup=get_kb("gender"), parse_mode="Markdown")
    else:
        if user['is_banned']:
            return await update.message.reply_text("🚫 Siz bloklangansiz!")
        await update_user(uid, state="menu", partner=None)
        await update.message.reply_text("Siz asosiy menyudasiz:", reply_markup=get_kb("menu"))

async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    text = update.message.text
    if not text: return

    user = await get_user(uid)
    if not user: return
    if user['is_banned']: return

    # Haqorat filtri
    if any(word in text.lower() for word in BAD_WORDS):
        await update.message.reply_text("❌ Odobsiz so'z ishlatmang!")
        return

    state = user['state']

    # STATE: GENDER
    if state == "gender":
        if text in ["👨 Erkak", "👩 Ayol"]:
            gender_val = "male" if "Erkak" in text else "female"
            await update_user(uid, gender=gender_val, state="menu")
            await update.message.reply_text("✅ Saqlandi! Endi suhbatdosh qidirishingiz mumkin.", reply_markup=get_kb("menu"))
        return

    # STATE: MENU
    elif state == "menu":
        if text == "🎲 Random qidirish":
            await update_user(uid, state="searching", search_pref="random")
            await update.message.reply_text("🔍 **Suhbatdosh qidirilyapti...**", reply_markup=get_kb("searching"), parse_mode="Markdown")
            await find_match(uid, context)
        elif text == "🔎 Jins bo‘yicha qidirish":
            await update_user(uid, state="search_gender")
            await update.message.reply_text("Kim bilan gaplashmoqchisiz?", reply_markup=get_kb("search_gender"))
        elif text == "🔄 Jinsni o‘zgartirish":
            await update_user(uid, state="gender")
            await update.message.reply_text("Jinsingizni tanlang:", reply_markup=get_kb("gender"))

    # STATE: SEARCH_GENDER
    elif state == "search_gender":
        if text == "🔙 Orqaga":
            await update_user(uid, state="menu")
            await update.message.reply_text("Menyu:", reply_markup=get_kb("menu"))
        elif text in ["👨 O‘g‘il qidirish", "👩 Qiz qidirish"]:
            pref = "male" if "O‘g‘il" in text else "female"
            await update_user(uid, state="searching", search_pref=pref)
            await update.message.reply_text("🔍 **Qidiruv boshlandi...**", reply_markup=get_kb("searching"), parse_mode="Markdown")
            await find_match(uid, context)

    # STATE: SEARCHING
    elif state == "searching":
        if text == "❌ Bekor qilish":
            pref = user['search_pref']
            my_gender = user['gender']
            q_key = f"queue:{my_gender}" if pref != "random" else "queue:random"
            await r.lrem(q_key, 0, str(uid))
            await update_user(uid, state="menu")
            await update.message.reply_text("❌ Qidiruv bekor qilindi.", reply_markup=get_kb("menu"))

    # STATE: CHAT
    elif state == "chat":
        partner_id = user['partner']
        if not partner_id:
            await update_user(uid, state="menu")
            return

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
            p_data = await get_user(partner_id)
            new_reports = p_data['reports'] + 1
            await update_user(partner_id, reports=new_reports)
            if new_reports >= 20:
                await update_user(partner_id, is_banned=True)
            
            await update_user(uid, state="menu", partner=None)
            await update_user(partner_id, state="menu", partner=None)
            await update.message.reply_text("🚨 Shikoyat yuborildi, chat yopildi.", reply_markup=get_kb("menu"))
            await context.bot.send_message(partner_id, "🚨 Siz ustingizdan shikoyat tushdi, chat yopildi.", reply_markup=get_kb("menu"))
        
        else:
            # Xabarni sherigiga yuborish
            try:
                await context.bot.send_message(partner_id, text)
            except:
                await update_user(uid, state="menu", partner=None)
                await update.message.reply_text("⚠️ Aloqa uzildi.", reply_markup=get_kb("menu"))

def main():
    if not TOKEN:
        print("XATO: BOT_TOKEN o'rnatilmagan!")
        return

    app = Application.builder().token(TOKEN).build()
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))
    
    async def post_init(application):
        await init()
        # Conflict xatosini oldini olish uchun webhookni tozalash
        await application.bot.delete_webhook(drop_pending_updates=True)
        print("🚀 Bot ishga tushdi!")

    app.post_init = post_init
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()